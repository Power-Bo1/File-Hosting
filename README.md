# Client File Host on Kubernetes (bare metal)

A two-image app on a kubeadm cluster with MetalLB:

- **Image 1 (`web/`)** — a website where clients upload and list files (Flask).
- **Image 2 (`api/`)** — the API that stores files on a volume and records
  metadata in PostgreSQL (FastAPI).
- **PostgreSQL** — internal-only database (ClusterIP Service = the "tunnel").
- **Grafana + Loki + Alloy** — real-time logging across the whole cluster.

```
                 MetalLB IP (LAN)
                       |
                  [ Ingress ]  host: files.local
                       |
        [ web x2 ] --calls--> [ api ] --ClusterIP--> [ postgres ] -- PVC
        (Image 1)             (Image 2, +PVC)        (internal only)

   Grafana + Loki + Alloy  -- collect logs from every pod, both nodes
```

## Repository layout

```
file-host-k8s/
├── README.md                 # you are here: deploy, verify, release history
├── UPGRADE.md                # per-release detail and migration runbooks
├── deploy.sh                 # cluster-side apply/install (kubectl + helm)
├── .gitignore                # keeps k8s/*-secret.yaml out of version control
├── api/                      # Image 2 — FastAPI
│   ├── Dockerfile            # distroless, non-root, digest-pinned
│   ├── Dockerfile.python-slim  # pre-v2.6 fallback
│   ├── app.py  security.py  mailer.py  requirements.txt
├── web/                      # Image 1 — Flask, zero JavaScript
│   ├── Dockerfile  Dockerfile.python-slim
│   └── web.py  requirements.txt
├── k8s/
│   ├── namespace.yaml              # apply FIRST
│   ├── postgres-secret.yaml        # gitignored  (+ .example.yaml template)
│   ├── auth-secret.yaml            # JWT + Flask keys      (+ .example)
│   ├── smtp-secret.yaml            # real mail provider    (+ .example)
│   ├── grafana-secret.yaml         # Grafana admin         (+ .example)
│   ├── postgres.yaml               # pvc + deployment + svc
│   ├── api.yaml  web.yaml          # the two app tiers
│   ├── mailpit.yaml                # in-cluster SMTP catcher
│   ├── registry.yaml               # in-cluster image registry
│   ├── cert-manager-ca-issuer.yaml # lab CA + ClusterIssuer
│   ├── ingress-nginx-headers.yaml  # CSP, HSTS, CORP, Referrer-Policy …
│   ├── nfs-storageclass.yaml       # ReadWriteMany via csi-driver-nfs
│   ├── api-files-rwx.yaml          # RWX claims for uploads + backups
│   ├── api-files-pvc-legacy.yaml   # pre-v2.11 RWO volume, kept for rollback
│   ├── migrate-files-to-rwx.yaml   # one-shot RWO -> RWX copy, checksummed
│   ├── postgres-backup.yaml        # nightly pg_dump CronJob + PVC
│   ├── files-backup.yaml           # nightly blob tar CronJob + PVC
│   ├── backup-shell.yaml           # helper pod: DB restore / copy out
│   ├── files-restore-shell.yaml    # helper pod: blob restore / copy out
│   ├── traefik-https-redirect.yaml # only if you run Traefik
│   └── alloy-rbac.yaml             # lets Alloy read pod logs
├── helm/
│   └── loki-values.yaml  grafana-values.yaml  alloy-values.yaml
├── scripts/
│   ├── build-test.sh         # build + prove both images BEFORE pushing
│   ├── check-app.sh          # pods, endpoints, DNS, /data perms, schema, backups
│   ├── check-headers.sh      # TLS chain, security headers, app-vs-CSP match
│   ├── restore-drill.sh      # prove the newest DB backup actually restores
│   └── validate_k8s.py       # static cross-checks across every manifest
└── tests/                    # 123 tests — see tests/README.md
```

## Prerequisites

- A kubeadm cluster: 1 control plane + 2 workers. Pod CIDR **must not
  overlap your LAN or VM network** — use `--pod-network-cidr=10.244.0.0/16`,
  not `192.168.0.0/16`. See the v2.4 notes in UPGRADE.md for why.
- **Calico** (via the Tigera operator) as the CNI.
- **MetalLB** with a pool, e.g. `192.168.1.200-192.168.1.220`.
- **ingress-nginx** as the ingress controller. (Traefik works too — set
  `ingressClassName: traefik` in `k8s/web.yaml` and `INGRESS_CLASS=traefik`
  for `deploy.sh`, and apply `k8s/traefik-https-redirect.yaml`.)
- **cert-manager** for TLS.
- **local-path-provisioner** as the default StorageClass (PostgreSQL).
- **csi-driver-nfs** plus an NFS export, for the ReadWriteMany volumes
  added in v2.11. Requires `nfs-common` on every node.
- Fixed addresses used throughout: ingress `192.168.1.200`, Grafana
  `.201`, registry `.210`, Mailpit `.211`. Adjust if your pool differs.
- Each node needs **4 GB+ RAM** — the full stack exceeds the 2 GB minimum.
- A Docker ID and `docker login dhi.io` on the build machine, for the
  hardened base images (free, no subscription).

---

## Part 1 — Storage provisioner

Bare-metal kubeadm has no default storage, so PVCs hang in `Pending`.
(Use the newest tag from the local-path-provisioner releases page.)

```bash
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
kubectl patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

## Part 2 — Registry

### 2.1 Deploy it

```bash
kubectl apply -f k8s/registry.yaml
kubectl get svc -n registry      # EXTERNAL-IP should be 192.168.1.210
```

### 2.2 Trust it on EVERY node (master + both workers)

```bash
sudo mkdir -p /etc/containerd/certs.d/192.168.1.210:5000
cat <<EOF | sudo tee /etc/containerd/certs.d/192.168.1.210:5000/hosts.toml
server = "http://192.168.1.210:5000"
[host."http://192.168.1.210:5000"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
EOF
```

Confirm `/etc/containerd/config.toml` contains:

```toml
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
```

Then: `sudo systemctl restart containerd`

## Part 3 — Build & push the two images

On your build machine, allow the insecure registry. For Docker, add to
`/etc/docker/daemon.json` then `sudo systemctl restart docker`:

```json
{ "insecure-registries": ["192.168.1.210:5000"] }
```

Build and push:

```bash
./scripts/build-test.sh          # builds BOTH images and proves them locally
docker push 192.168.1.210:5000/file-api:2.13
docker push 192.168.1.210:5000/file-web:2.13
```

(`nerdctl build` + `nerdctl --insecure-registry push` works the same way.)

## Part 4-6 — Deploy app + logging

Either run everything with the helper script:

```bash
REGISTRY_IP=192.168.1.210 INGRESS_CLASS=nginx ./deploy.sh
```

…or apply manually in order:

```bash
# Secrets first — postgres.yaml has held no credentials since v2.4.
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres-secret.yaml -f k8s/auth-secret.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/mailpit.yaml
kubectl apply -f k8s/api.yaml
kubectl apply -f k8s/web.yaml
kubectl apply -f k8s/postgres-backup.yaml -f k8s/files-backup.yaml
kubectl apply -f k8s/ingress-nginx-headers.yaml

kubectl create namespace logging
helm repo add grafana https://grafana.github.io/helm-charts && helm repo update
helm install loki    grafana/loki    -n logging -f helm/loki-values.yaml
helm install grafana grafana/grafana -n logging -f helm/grafana-values.yaml
helm install alloy   grafana/alloy   -n logging -f helm/alloy-values.yaml
kubectl apply -f k8s/alloy-rbac.yaml
kubectl rollout restart daemonset alloy -n logging
```

## Part 7 — Use it

```bash
echo "192.168.1.200 files.local" | sudo tee -a /etc/hosts   # ingress IP
```

Open <http://files.local>, upload a file, see it listed. Verify in the DB:

```bash
kubectl exec -it -n fileapp deploy/postgres -- \
  psql -U appuser -d clientfiles -c "SELECT client, filename, size, uploaded_at FROM files;"
```

## Part 8 — Logs in Grafana

```bash
kubectl get svc -n logging grafana    # browse to its EXTERNAL-IP
```

Log in as `admin`, go to **Explore → Loki**, and query:

```logql
{namespace="fileapp"}                       # all app logs
{namespace="fileapp", container="api"}      # just the API
{namespace="fileapp"} |= "stored"           # successful uploads
```

---

## Scaling & production notes

- **File storage is ReadWriteMany over NFS since v2.11**, so the API runs
  two replicas across two nodes with rolling (zero-downtime) deploys.
  PostgreSQL deliberately stays on ReadWriteOnce local disk — NFS locking
  and fsync semantics are unsafe for a database.
- **PostgreSQL** here is one replica with no backups. Use a StatefulSet,
  add `pg_dump`/WAL backups, and consider an operator (CloudNativePG) for real use.
- **Secrets** are plain `stringData`. Use Sealed Secrets or an external
  secrets manager in production.
- **Registry** is insecure HTTP — fine on a trusted LAN, not the internet.
- Add **Prometheus** alongside this for CPU/memory metrics; add
  **cert-manager** for HTTPS on `files.local`.

## Troubleshooting

| Symptom | Check |
|---|---|
| PVC `Pending` | local-path provisioner missing/not default (`kubectl get storageclass`) |
| `ImagePullBackOff` | containerd `certs.d` config missing on that node, or image not pushed; test with `sudo crictl pull 192.168.1.210:5000/file-api:2.13` |
| API `CrashLoopBackOff` | DB unreachable — `kubectl logs -n fileapp deploy/api`; check Secret + that postgres is Ready |
| Upload 500 | API can't write `/data` or reach Postgres |
| Empty file table on web | `API_URL` wrong or API has no endpoints (`kubectl get endpoints -n fileapp api`) |
| Grafana IP `<pending>` | MetalLB pool exhausted or overlaps another IP |
| No logs in Loki | Alloy RBAC (`pods/log`) missing or Loki URL wrong — `kubectl logs -n logging daemonset/alloy` |

---

## v2: users + HTTPS

Version 2.0 adds user accounts (register/login, bcrypt + JWT, per-user
file isolation) and HTTPS on `files.local` via cert-manager with a lab
CA. See **UPGRADE.md** for the step-by-step, including how to trust the
CA on your workstation.

### Let's Encrypt sidebar (when you own a real domain)

`files.local` can't get a Let's Encrypt cert (LE must reach your domain
from the internet). With a real domain + public DNS + port 80 open,
replace the lab issuer with:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
    - http01:
        ingress:
          ingressClassName: traefik
```

…and set `cert-manager.io/cluster-issuer: letsencrypt-prod` on the
Ingress. Behind home NAT without port-forwarding, use a DNS-01 solver
with your DNS provider's API token instead.

---

## Release history — v2.4 to v2.13

Summaries only. **`UPGRADE.md` carries the detail**: what broke, why the
fix is shaped the way it is, the migration commands, and the honest
limitations each release did *not* close.

"Rebuild?" means the container images changed and must be rebuilt and
pushed. Where it says *no*, applying the manifests is enough.

| Version | Change | Rebuild? |
|---|---|---|
| **v2.4** | Credentials moved out of manifests into gitignored `k8s/*-secret.yaml` with committed `.example` templates; `namespace.yaml` split out; Grafana switched to `admin.existingSecret`. Also recorded the **pod-CIDR migration** (192.168.0.0/16 → 10.244.0.0/16) that fixed all pod→LAN egress. | no |
| **v2.5** | Container hardening: multi-stage builds, non-root fixed UID, read-only root filesystem, all capabilities dropped, seccomp, resource limits, `.dockerignore`, `PYTHONUNBUFFERED` so logs reach Loki in real time. | **yes** |
| **v2.6** | Base image moved to Docker Hardened Images (distroless, 0 CVEs, UID 65532), digest-pinned, dependencies in a venv. **v2.6.1** stripped pip, which the venv had silently reintroduced. **v2.6.2** fixed a build gate that reported PASSED while skipping checks. | **yes** |
| **v2.7** | Security response headers at the ingress: CSP, HSTS, CORP, COOP, Referrer-Policy, Permissions-Policy, nosniff. **v2.7.1** added `upgrade-insecure-requests`. **v2.7.2** taught `check-headers.sh` to compare the running app against the deployed CSP. | no |
| **v2.8** | Per-token JWT revocation — logout now revokes *that* session via a `jti` denylist, while password reset still kills all of them. The check rides the DB lookup the request already made, so it costs nothing. **v2.8.1** fixed a backup check that passed on a 14-day-old backup. | **yes** |
| **v2.9** | Users can download and delete their own files. Downloads are always `octet-stream` + attachment, so an uploaded `.html` can never render in the app's origin. Delete removes the row first, then the blob, in one transaction. | **yes** |
| **v2.10** | Nightly **file-blob backups** alongside the existing `pg_dump`, both verified at creation. `check-app.sh` gained a database↔disk reconciliation that finds rows with no blob and blobs with no row. | no |
| **v2.11** | **ReadWriteMany storage over NFS.** Unblocked three things at once: two API replicas across two nodes, `RollingUpdate` instead of `Recreate`, and backup jobs no longer pinned to one node. Includes a checksum-verified migration Job. **v2.11.1** added backup **staleness** detection. | no |
| **v2.12** | Professional frontend, still zero JavaScript. CSS moved to an external stylesheet, which let the CSP drop `'unsafe-inline'`. **v2.12.1** made the CSS guarantees enforceable by test and closed the font/connect channels. | **yes** |
| **v2.13** | HTML-layer hardening: **CSRF tokens** on every state-changing request, `__Host-` session cookie, `Cache-Control: no-store` on HTML, right-to-left-override and control characters stripped from filenames, ASCII-only usernames, `Cross-Origin-Resource-Policy`. | **yes** |

### Where the current state is documented

- **`UPGRADE.md`** — per-release detail, migration runbooks and rollbacks.
- **`scripts/`** — the four verification scripts; each *asserts* rather
  than reports, and exits non-zero on failure.
- **`tests/`** — 123 tests. `python3 scripts/validate_k8s.py` adds 37
  static manifest cross-checks.

### Deploying the current version from scratch

```bash
for f in postgres-secret auth-secret grafana-secret; do
  cp k8s/$f.example.yaml k8s/$f.yaml
done
# edit each one — auth-secret wants: openssl rand -hex 32, twice

chmod +x scripts/*.sh deploy.sh      # some unzip tools drop the exec bit
docker login dhi.io                  # hardened base images
./scripts/build-test.sh              # build + prove both images locally
docker push 192.168.1.210:5000/file-api:2.13
docker push 192.168.1.210:5000/file-web:2.13

REGISTRY_IP=192.168.1.210 INGRESS_CLASS=nginx ./deploy.sh
```

Then verify — on the wire, not by assumption:

```bash
python3 scripts/validate_k8s.py   # 37 manifest cross-checks
./scripts/check-app.sh            # pods, endpoints, DNS, /data, schema, backups
./scripts/check-headers.sh        # TLS chain, headers, app-vs-CSP compatibility
./scripts/restore-drill.sh        # prove the newest backup restores
```

Apply order matters in two places: **namespace → secrets → workloads**,
and the **web image must be deployed before the tightened CSP** (an older
image with inline `<style>` renders unstyled under `style-src 'self'`).
