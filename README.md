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
├── README.md
├── deploy.sh              # cluster-side apply/install (kubectl + helm)
├── api/                   # Image 2 (FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── web/                   # Image 1 (Flask)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── web.py
├── k8s/
│   ├── registry.yaml      # in-cluster image registry (MetalLB)
│   ├── postgres.yaml      # namespace + secret + pvc + deployment + svc
│   ├── api.yaml           # pvc + deployment + svc (Image 2)
│   ├── web.yaml           # deployment + svc + ingress (Image 1)
│   └── alloy-rbac.yaml    # lets Alloy read pod logs
└── helm/
    ├── loki-values.yaml
    ├── grafana-values.yaml
    └── alloy-values.yaml
```

## Assumptions

- A working kubeadm cluster (1 control plane + 2 workers) with a CNI.
- **MetalLB** installed with a pool, e.g. `192.168.1.200-192.168.1.220`.
- An **ingress controller** installed (Traefik by default; set
  `ingressClassName: nginx` in `k8s/web.yaml` and `INGRESS_CLASS=nginx`
  for `deploy.sh` if you use ingress-nginx).
- The ingress LoadBalancer IP is `192.168.1.200`; the registry is pinned
  to `192.168.1.210`. Adjust if your pool differs (keep them distinct).
- Each node has **4 GB+ RAM** — the full stack exceeds the 2 GB minimum.

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
docker build -t 192.168.1.210:5000/file-api:1.0 api && docker push 192.168.1.210:5000/file-api:1.0
docker build -t 192.168.1.210:5000/file-web:1.0 web && docker push 192.168.1.210:5000/file-web:1.0
```

(`nerdctl build` + `nerdctl --insecure-registry push` works the same way.)

## Part 4-6 — Deploy app + logging

Either run everything with the helper script:

```bash
REGISTRY_IP=192.168.1.210 INGRESS_CLASS=traefik ./deploy.sh
```

…or apply manually in order:

```bash
# Change the password in k8s/postgres.yaml first!
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/api.yaml
kubectl apply -f k8s/web.yaml

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

- **File storage is ReadWriteOnce**, so the API is single-replica. To scale
  it across both nodes, use ReadWriteMany storage (NFS) or object storage
  (MinIO/S3) for the blobs. Postgres metadata is not the bottleneck.
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
| `ImagePullBackOff` | containerd `certs.d` config missing on that node, or image not pushed; test with `sudo crictl pull 192.168.1.210:5000/file-api:1.0` |
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

## v2.4: secrets in their own files

Credentials live in `k8s/*-secret.yaml`, which are **gitignored**. Copy
the templates and fill them in before deploying:

```bash
for f in postgres-secret auth-secret grafana-secret; do
  cp k8s/$f.example.yaml k8s/$f.yaml
done
# then edit each one (auth-secret wants: openssl rand -hex 32, twice)
```

After unzipping, restore the exec bit on the helper scripts (some
unzip versions drop it):

```bash
chmod +x scripts/*.sh deploy.sh
```

Verification scripts:

```bash
./scripts/check-app.sh       # pods, endpoints, DNS timing, /data perms, schema
./scripts/check-headers.sh   # TLS chain + security response headers
./scripts/build-test.sh      # build and prove the images before pushing
./scripts/restore-drill.sh   # prove the newest DB backup restores
```

Apply order: `namespace.yaml` -> secrets -> workloads. `deploy.sh`
handles it and refuses to run if a secret file is missing or still
contains CHANGE_ME. See UPGRADE.md for the full v2.4 notes, including
the pod CIDR migration.
