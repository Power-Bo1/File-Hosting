# Upgrade v1 -> v2: users + HTTPS

What changes:

- **Database**: new `users` table; `files` gains a `user_id` column
  (migrations run automatically at API startup, idempotent).
- **API (file-api:2.0)**: `POST /auth/register`, `POST /auth/login`
  (bcrypt password hashing, JWT issuance); `/upload` and `/files` now
  require `Authorization: Bearer <token>`; files stored per-user.
- **Web (file-web:2.0)**: login/register pages; the JWT lives in a
  signed Flask session cookie; users only see their own files.
- **HTTPS**: cert-manager + a lab CA issue a TLS cert for
  `files.local`; the ingress terminates TLS.

```
Browser ── HTTPS ──> Ingress ── HTTP ──> web ── HTTP+JWT ──> api ──> postgres
            (TLS terminates here; in-cluster hops stay on the pod network)
```

## Phase A - secrets

```bash
openssl rand -hex 32   # JWT_SECRET
openssl rand -hex 32   # FLASK_SECRET_KEY
nano k8s/auth-secret.yaml      # paste both values
kubectl apply -f k8s/auth-secret.yaml
```

## Phase B - build & push v2 images

```bash
docker build -t 192.168.1.210:5000/file-api:2.0 api && docker push 192.168.1.210:5000/file-api:2.0
docker build -t 192.168.1.210:5000/file-web:2.0 web && docker push 192.168.1.210:5000/file-web:2.0
curl http://192.168.1.210:5000/v2/file-api/tags/list   # must show "2.0"
```

## Phase C - roll out the app

```bash
kubectl apply -f k8s/postgres.yaml     # includes the probe fix (-d clientfiles)
kubectl apply -f k8s/api.yaml
kubectl -n fileapp rollout status deploy/api --timeout=180s
kubectl apply -f k8s/web.yaml          # ingress TLS will stay pending until Phase D
kubectl -n fileapp rollout status deploy/web --timeout=180s
```

Verify the users table exists:

```bash
kubectl exec -it -n fileapp deploy/postgres -- \
  psql -U appuser -d clientfiles -c "\dt"
```

## Phase D - HTTPS

1. Install cert-manager and wait for all three deployments:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.20.2/cert-manager.yaml
kubectl get pods -n cert-manager     # 3/3 Running
```

2. Create the lab CA chain:

```bash
kubectl apply -f k8s/cert-manager-ca-issuer.yaml
kubectl get clusterissuer            # both Ready=True
```

3. The web Ingress (applied in Phase C) already carries the
   `cert-manager.io/cluster-issuer: lab-ca-issuer` annotation and a
   `tls:` block, so the cert is issued automatically:

```bash
kubectl get certificate -n fileapp   # files-local-tls  READY True
curl -k https://files.local          # login page over TLS
```

4. Trust the CA on your workstation (kills browser warnings):

```bash
kubectl get secret -n cert-manager lab-ca-secret \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > lab-ca.crt

# Ubuntu/Debian:
sudo cp lab-ca.crt /usr/local/share/ca-certificates/lab-ca.crt
sudo update-ca-certificates
# macOS: open Keychain Access -> System -> import -> Always Trust
# Windows: double-click -> Install -> Trusted Root Certification Authorities
# Firefox (any OS): Settings -> Privacy & Security -> Certificates -> Import
```

5. Harden the cookie + (optional) force HTTPS:

```bash
# set COOKIE_SECURE to "true" in k8s/web.yaml, then:
kubectl apply -f k8s/web.yaml

# Traefik-only HTTP->HTTPS redirect:
kubectl apply -f k8s/traefik-https-redirect.yaml
# then uncomment the router.middlewares annotation in k8s/web.yaml and re-apply
# (ingress-nginx redirects automatically once tls: exists)
```

## Verification checklist

```bash
# 1. Register + login via https://files.local in a browser
# 2. Upload a file; it appears in YOUR list
# 3. Register a second user; their list is empty (isolation works)
# 4. API rejects anonymous calls:
kubectl run curltest --rm -it --restart=Never -n fileapp \
  --image=curlimages/curl -- \
  curl -s -o /dev/null -w "%{http_code}\n" http://api:8000/files
#    -> 403 (no token); a garbage token returns 401
# 5. DB has the user:
kubectl exec -it -n fileapp deploy/postgres -- \
  psql -U appuser -d clientfiles -c "SELECT id, username, created_at FROM users;"
```

## Note on pre-v2 uploads

Files uploaded before v2 have `user_id = NULL` and are invisible to
everyone. To hand them to an account:

```sql
UPDATE files SET user_id = (SELECT id FROM users WHERE username = 'YOURNAME')
WHERE user_id IS NULL;
```

## Security notes (lab vs production)

- JWTs are 60-min bearer tokens with no refresh/revocation. Fine for a
  lab; production wants refresh tokens or server-side sessions.
- TLS terminates at the ingress; web->api->postgres hops are plain HTTP
  on the pod network. Next steps if needed: TLS on Postgres, or a
  service mesh / NetworkPolicies.
- The lab CA's private key lives in the cluster (lab-ca-secret).
  Anyone with cluster-admin can mint certs your machines trust - fine
  for a homelab, not for shared infra.
- With a real public domain, swap lab-ca-issuer for a Let's Encrypt
  ACME ClusterIssuer (see README sidebar).

---

# v2.1 - production-hardening pass

Bugs found in review/testing and fixed (build images as **:2.1**):

| # | Severity | Bug | Fix |
|---|----------|-----|-----|
| 1 | High | ingress-nginx rejects uploads >1 MB (default body limit) -> 413 | `proxy-body-size: 120m` annotation on the Ingress |
| 2 | High | DB connections leaked on any request error -> can exhaust Postgres `max_connections` | all DB access through a commit/rollback/ALWAYS-close context manager |
| 3 | High | No upload size cap -> one client can fill the PVC | streamed writes with `MAX_UPLOAD_MB` cap (413), enforced in API, web, and ingress |
| 4 | High | Web frontend crashed (500) if the API was down or returned non-JSON | every API call goes through a guarded helper; friendly error banners |
| 5 | Med | Re-uploading the same filename overwrote the disk file but inserted a duplicate DB row | unique index (user_id, filename) + UPSERT; startup migration de-dupes old rows |
| 6 | Med | Interrupted upload left a half-written file at the final path | write to `.part` temp file, atomic `os.replace` on success, unlink on failure |
| 7 | Med | Cross-site POST could ride the session cookie (CSRF) | `SameSite=Lax` + `HttpOnly` + `Secure` cookie flags |
| 8 | Med | API pod stayed Ready with the DB down -> uploads 500ed | new `/ready` endpoint checks the DB; readiness probe moved to it; liveness stays on `/health` |
| 9 | Med | No resource requests/limits -> one runaway pod can starve a node | requests/limits on api, web, postgres, registry |
| 10 | Med | `deploy.sh` ignored `INGRESS_CLASS`; manifest hardcoded the wrong class (the 404 you hit) | class baked to `nginx`, script now rewrites it on apply |
| 11 | Low | Login timing leaked whether a username exists | dummy-hash compare for unknown users |
| 12 | Low | Filenames `""`/`.`/`..`/NUL could 500 or misbehave | `sanitize_filename()` with tests |
| 13 | Low | Postgres had no liveness probe; deprecated FastAPI startup hook; unpinned Python deps | liveness probe added; lifespan API; `~=` version pins |

Also new: `tests/` (34 tests; see tests/README.md) and
`scripts/validate_k8s.py` (static manifest cross-checks - run it after
any YAML edit).

## Rolling out v2.1

```bash
docker build -t 192.168.1.210:5000/file-api:2.1 api && docker push 192.168.1.210:5000/file-api:2.1
docker build -t 192.168.1.210:5000/file-web:2.1 web && docker push 192.168.1.210:5000/file-web:2.1

python3 scripts/validate_k8s.py          # should end: 0 failures
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/api.yaml
kubectl apply -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/api && kubectl -n fileapp rollout status deploy/web
```

Verify: upload a 5 MB file (used to 413 at the ingress), then
`kubectl exec` a 2nd upload of the same name and confirm a single row in
`files`. Run the full test suite locally:
`pip install -r api/requirements.txt -r web/requirements.txt -r requirements-dev.txt && pytest tests/ -v`

## Known gaps v2.1 does NOT close (honest list)

- Postgres: single replica, no backups (add pg_dump CronJob or CloudNativePG)
- No JWT refresh/revocation; logout only clears the cookie, the token
  stays valid until expiry (60 min)
- No login rate limiting (enable the commented `limit-rps` annotation)
- web -> api -> postgres hops are plain HTTP inside the cluster
  (NetworkPolicies / TLS-to-Postgres are the next layer)
- Registry is insecure HTTP on a trusted LAN only
- Images pinned by tag, not digest; base images (`python:3.12-slim`,
  `postgres:17`) update underneath you

---

# v2.1.1 - readiness probe hotfix (CrashLoopBackOff)

**Symptom:** after deploying v2.1, the `api` pod ran but stayed `0/1`
Ready; the kubelet logged `Readiness probe failed: Get .../ready:
context deadline exceeded`, while `/health` returned 200 and a manual
`psycopg2.connect(..., connect_timeout=5)` from inside the pod succeeded.

| # | Severity | Bug | Fix |
|---|----------|-----|-----|
| 14 | High | The v2.1 `/ready` endpoint (new in 2.1) ran a blocking commit on a sync FastAPI thread, and `db()` had no `connect_timeout` - so any momentary DB stall hung the probe thread with no deadline, tripping the 1s probe timeout and pinning the pod at 0/1 Ready (Service then refuses traffic -> "Cannot reach the API"). | `db()` now passes `connect_timeout` (default 5s); `/ready` uses an autocommit read with explicit close (no COMMIT round-trip to stall on); readiness probe gets `timeoutSeconds: 5` + `failureThreshold: 3`. |

**Why the symptom looked contradictory:** `/health` doesn't touch the DB,
so it stayed fast. The manual test set `connect_timeout` and never
committed, so it returned instantly. `/ready` did neither - it was the
only path that both opened a connection without a deadline *and*
committed on a blocking thread.

New regression tests (in tests/test_api.py):
- `test_ready_returns_within_budget` - /ready must return ~instantly
- `test_ready_returns_503_when_db_down` - DB down -> 503, never a hang
- `test_db_passes_connect_timeout` - db() must hand psycopg2 a
  connect_timeout (guards the root cause from regressing)

## Rolling out v2.1.1

Only the **API** image changed (web:2.1.1 is functionally identical to
2.1; it's re-tagged only to keep the bundle on one version). To fix a
running cluster you can rebuild just the API:

```bash
docker build -t 192.168.1.210:5000/file-api:2.1.1 api && docker push 192.168.1.210:5000/file-api:2.1.1
kubectl apply -f k8s/api.yaml
kubectl rollout status -n fileapp deploy/api --timeout=120s
```

Verify /ready returns fast from inside the pod, then confirm 1/1:

```bash
kubectl exec -it -n fileapp deploy/api -- \
  python3 -c "import urllib.request,time; t=time.time(); print(urllib.request.urlopen('http://localhost:8000/ready').read(), round(time.time()-t,2),'s')"
kubectl get pods -n fileapp      # api-xxxx  1/1  Running
```

---

# v2.2 - PostgreSQL backups (closing the unrecoverable gap)

New files:
- `k8s/postgres-backup.yaml` - PVC + nightly CronJob: `pg_dump -Fc`
  (compressed, custom format) -> readability check via `pg_restore --list`
  -> prune dumps older than `RETENTION_DAYS` (default 7).
- `k8s/backup-shell.yaml` - throwaway helper pod (postgres:17 + the
  backup PVC + DB creds) for browsing backups and running restores.

Design notes:
- `pg_dump` takes a **consistent snapshot while the app runs** (MVCC);
  no downtime, no need to stop uploads during backup.
- The job image is `postgres:17` so the dump client always matches the
  server major version.
- The job **verifies** every archive (`pg_restore --list`); a dump that
  can't be read fails the Job loudly instead of rotting silently.
- Preferred pod anti-affinity pushes backups onto a **different node**
  than Postgres, so one dead disk doesn't take DB + backups together.
- `concurrencyPolicy: Forbid` -> overlapping runs are impossible.

## Deploy

```bash
kubectl apply -f k8s/postgres-backup.yaml
kubectl get cronjob -n fileapp          # SCHEDULE 0 3 * * *
```

## Manual backup (also how you test the CronJob without waiting a day)

```bash
kubectl create job -n fileapp --from=cronjob/postgres-backup manual-$(date +%s)
kubectl logs -n fileapp -l app=postgres-backup -f
# expect: "OK: /backups/clientfiles-<stamp>.dump (<size>)" + a listing
```

## Restore DRILL (proves the backup is usable - touches nothing real)

```bash
kubectl apply -f k8s/backup-shell.yaml
kubectl exec -it -n fileapp backup-shell -- bash
# inside the pod:
ls -lh /backups
createdb -h $PGHOST -U $PGUSER clientfiles_drill
pg_restore -h $PGHOST -U $PGUSER -d clientfiles_drill /backups/clientfiles-<TAB-complete>.dump
psql -h $PGHOST -U $PGUSER -d clientfiles_drill -c "SELECT count(*) FROM users;" -c "SELECT count(*) FROM files;"
dropdb  -h $PGHOST -U $PGUSER clientfiles_drill
exit
kubectl delete pod -n fileapp backup-shell
```

Counts should match the live DB. Do this drill after the first backup
and then occasionally - an unrestored backup is a hope, not a backup.

## REAL restore (data loss / bad migration / oops-DELETE)

```bash
kubectl scale deploy/api -n fileapp --replicas=0     # stop writers
kubectl apply -f k8s/backup-shell.yaml
kubectl exec -it -n fileapp backup-shell -- bash -c \
  'pg_restore --clean --if-exists -h $PGHOST -U $PGUSER -d clientfiles /backups/<FILE>.dump'
kubectl scale deploy/api -n fileapp --replicas=1
kubectl delete pod -n fileapp backup-shell
```

`--clean --if-exists` drops and recreates objects, returning the DB to
the dump's state. Full disaster variant (Postgres PVC destroyed): apply
postgres.yaml, let it init an empty DB from the Secret, then the same
pg_restore (no scale-down needed - nothing is running yet).

## Off-cluster copy (protects against losing the whole cluster)

```bash
kubectl apply -f k8s/backup-shell.yaml
kubectl cp fileapp/backup-shell:/backups ./pg-backups-$(date +%Y%m%d)
kubectl delete pod -n fileapp backup-shell
```

## Honest limitations

- **File blobs are NOT covered.** pg_dump saves users + file *metadata*;
  the actual uploaded files live on `api-files-pvc`. After a
  DB-only restore, rows and blobs can drift (harmless orphans, or
  metadata for blobs uploaded after the dump). Covering blobs needs
  either off-cluster rsync of the node's local-path dir or moving blob
  storage to MinIO/S3 - a good next chapter.
- Backups live **inside the same cluster** until you run the
  off-cluster copy. Anti-affinity survives one dead node, not a dead
  cluster. Automate the copy (NFS/MinIO/rsync cron on a workstation)
  for real safety.
- Nightly dumps = up to 24h of data loss window. Point-in-time recovery
  (WAL archiving via pgBackRest/CloudNativePG) is the production answer
  if that window matters.

---

# v2.2.1 - foolproof restore drill

Field report: the manual drill instructions contained a placeholder
(`clientfiles-<your-file>.dump`); pasted literally, bash treats `<` and
`>` as redirection, aborts the line (`bash: your-file: No such file or
directory`), pg_restore never runs, and the empty drill DB then reports
`relation "users" does not exist`. Same error text as the old
missing-migrations bug, completely different cause: the drill database
was empty because the restore never executed. The backup itself was
fine.

Fix: `scripts/restore-drill.sh` - a one-command drill with no
placeholders to mis-paste:

```bash
./scripts/restore-drill.sh
```

It ensures the backup-shell pod is Ready (waits, so no "container not
found" from exec-ing too early), auto-selects the NEWEST dump, restores
into clientfiles_drill, prints drill-vs-live row counts (they may lag
live if data changed after the dump - informational, not a failure),
drops the scratch DB, and ends with `DRILL PASS`. Any failure exits
nonzero and loudly.

---

# v2.3 - "Forgot password" with email verification

Implements the full recovery flow: email at registration -> "Forgot
password?" on the login page -> time-limited single-use emailed link ->
new password -> ALL sessions force-logged-out -> confirmation email.

New/changed:
- **Schema** (auto-migrated at API startup): `users.email` (unique),
  `users.token_version`, new `password_resets` table (stores only
  SHA-256 hashes of tokens).
- **API**: `POST /auth/forgot`, `POST /auth/reset`; registration now
  requires a valid email; JWTs carry a `ver` claim checked against the
  DB on every authenticated request (password reset bumps it -> every
  older token dies instantly = "log out all sessions", finally closing
  the JWT-revocation gap).
- **Web**: forgot/reset pages, email field at registration.
- **Mailpit** (`k8s/mailpit.yaml`): in-cluster SMTP catcher; UI at
  http://192.168.1.211:8025 shows every email the app sends. Swap the
  SMTP_* env vars in k8s/api.yaml for a real provider later.
- **Images**: file-api:2.3, file-web:2.3. New api env: SMTP_HOST,
  SMTP_PORT, SMTP_FROM, APP_BASE_URL, RESET_TTL_MINUTES (30).

Security properties: hashed-at-rest tokens (a DB leak yields nothing
usable), 30-min TTL, single-use, one active link per user, generic
responses (no account enumeration), constant-ish-time login preserved,
reset links never logged (Loki ships every print!).

## Existing users (created before v2.3)

They have no email and cannot use the flow until one is set:

```bash
kubectl exec -it -n fileapp deploy/postgres -- psql -U appuser -d clientfiles \
  -c "UPDATE users SET email='you@example.com' WHERE username='YOURNAME';"
```

## Honest limitations

- No rate limit on /auth/forgot (email-bombing possible); the commented
  `limit-rps` ingress annotation is the quick damper.
- Authenticated requests now cost one extra small DB lookup (the
  token_version check) - the price of real session revocation.
- SMTP to Mailpit is plaintext in-cluster (fine for a catcher; use
  SMTP_STARTTLS=true + credentials for a real provider).
- SMS variant descoped (no SMS gateway in a homelab).

---

# v2.4 - credentials split out, plus the cluster-networking fixes

**No application code changed** - images stay `file-api:2.3` and
`file-web:2.3`. Nothing to rebuild or push; this release is manifests,
config and docs only.

## 1. Credentials moved into their own Secret files

| Was | Now |
|---|---|
| Secret embedded in `k8s/postgres.yaml` | `k8s/postgres-secret.yaml` |
| `adminUser`/`adminPassword` in `helm/grafana-values.yaml` | `k8s/grafana-secret.yaml`, referenced via `admin.existingSecret` |

Also new: `k8s/namespace.yaml` (the `fileapp` namespace, previously
inside postgres.yaml), a `*.example.yaml` template beside every secret,
and a `.gitignore` that excludes `k8s/*-secret.yaml` while keeping the
templates tracked.

The point is not that a separate file is "more secure" - the values are
still base64 in etcd either way. The point is that the credentials are
now in files you can keep OUT of version control, with committed
templates so the repo still describes what must be set. For encryption
at rest, the next step is Sealed Secrets or an external secret manager.

Apply order now matters: **namespace -> secrets -> workloads**.
`deploy.sh` does this, and fails fast if a secret file is missing or
still contains CHANGE_ME.

## 2. Folded in from the running cluster

- Mailpit back on its native port 1025 (`k8s/mailpit.yaml`, `api.yaml`,
  `api/mailer.py`), `SMTP_STARTTLS=false`. Note `containerPort` is
  documentation only - it does not tell the process which port to bind.
- `dnsConfig` (ndots:2, timeout:2, attempts:2) on the api and web
  Deployments - stops search-list expansion delays.

## 3. Cluster networking notes (not files - environment)

- **Pod CIDR migrated** `192.168.0.0/16` -> `10.244.0.0/16` via a Calico
  two-pool migration. The old pool overlapped the VM network
  (192.168.190.x) and the MetalLB/LAN range (192.168.1.x); Calico skips
  SNAT for in-pool destinations, so pods could not reach ANY 192.168.x.x
  host. Symptoms included 4s DNS stalls, "Cannot reach the API", and a
  cert-manager-cainjector crash loop (42 restarts) that self-healed the
  moment the migration completed.
- `--cluster-cidr` on kube-controller-manager and the node `podCIDR`
  fields intentionally remain 192.168.0.0/16: `podCIDR` is immutable,
  and Calico IPAM does not use it. Changing only the flag makes
  kube-controller-manager CrashLoopBackOff with "cidr ... is out the
  range of cluster cidr".
- **If this cluster is ever rebuilt**: `kubeadm init
  --pod-network-cidr=10.244.0.0/16`.
- CoreDNS forwards to `192.168.190.2` (the VMware NAT DNS). If the VMs
  move to bridged networking or real hardware, update the coredns
  ConfigMap or pod DNS breaks.

## Migrating a running cluster to v2.4

```bash
# 1. Secrets first (same values as before -> no behaviour change)
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres-secret.yaml
kubectl apply -f k8s/grafana-secret.yaml

# 2. Workloads (postgres.yaml no longer carries the Namespace/Secret)
kubectl apply -f k8s/postgres.yaml -f k8s/api.yaml -f k8s/web.yaml -f k8s/mailpit.yaml

# 3. Grafana picks up the Secret
helm upgrade grafana grafana/grafana -n logging -f helm/grafana-values.yaml

# 4. Verify
kubectl get secret -n fileapp postgres-secret
kubectl get secret -n logging grafana-admin-secret
kubectl get pods -n fileapp
python3 scripts/validate_k8s.py     # expect 0 failures
```

Log in to Grafana with the same credentials. If the password does not
take (Grafana keeps it in its own DB after first start):

```bash
kubectl exec -n logging deploy/grafana -- \
  grafana cli admin reset-admin-password '<password>'
```

---

# v2.5 - hardened container images

Images rebuild as **file-api:2.5 / file-web:2.5**. App code is unchanged;
only the build and runtime posture changed.

## Dockerfile changes (api + web)

| Change | Why |
|---|---|
| Multi-stage build (`builder` -> runtime) | pip/build tooling never ships in the final image |
| `apt-get upgrade -y` in the runtime stage | patches base-image CVEs at build time |
| Non-root user at a **fixed UID/GID 10001** | a fixed UID is required so k8s `runAsUser`/`fsGroup` can match; `adduser --system` picks an unpredictable UID |
| `python -m pip uninstall -y pip setuptools` | a process with RCE cannot install packages |
| `COPY --chown=appuser:appgroup *.py ./` | app files owned by the runtime user; no stray files (api previously used `COPY . .`) |
| `.dockerignore` | keeps `__pycache__`, `.venv`, `.git`, tests out of the build context |
| `PYTHONDONTWRITEBYTECODE=1` | nothing tries to write `.pyc` under a read-only root |
| `PYTHONUNBUFFERED=1` | **print() reaches stdout immediately -> Loki gets logs in real time** instead of block-buffered |
| `HEALTHCHECK` | for plain `docker run`; Kubernetes ignores it and uses the probes |

Base images are still tagged, not digest-pinned. To pin (recommended):

```bash
docker pull python:3.12-slim
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# then in both Dockerfiles: FROM python:3.12-slim@sha256:<digest> AS builder
```

## Kubernetes changes - REQUIRED, not optional

A non-root image alone would break uploads: UID 10001 cannot write to a
PVC owned by root. `k8s/api.yaml` and `k8s/web.yaml` now carry:

```yaml
      securityContext:            # pod level
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001            # kubelet chowns /data to this GID
        seccompProfile: { type: RuntimeDefault }
```
```yaml
        securityContext:          # container level
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: { drop: ["ALL"] }
```

`readOnlyRootFilesystem` needs writable scratch space, so both pods mount
an `emptyDir` at `/tmp`: the api spools multipart uploads there, and
gunicorn keeps worker heartbeat files there (`--worker-tmp-dir /tmp`).

`scripts/validate_k8s.py` now WARNs if a first-party container is missing
any of those three container-level settings.

## Deliberately NOT hardened

- **postgres** - the official image starts as root and drops to the
  postgres user via gosu; forcing `runAsNonRoot` breaks initialization
  and PGDATA ownership. A working database is worth more than a
  cosmetic win here. Use the CloudNativePG operator if this matters.
- **mailpit / registry / chart-installed pods** (Loki, Grafana, Alloy,
  ingress-nginx, cert-manager) - third-party images with their own
  expectations; harden via their chart values, not by patching here.

## Rolling out

```bash
docker build -t 192.168.1.210:5000/file-api:2.5 api && docker push 192.168.1.210:5000/file-api:2.5
docker build -t 192.168.1.210:5000/file-web:2.5 web && docker push 192.168.1.210:5000/file-web:2.5

python3 scripts/validate_k8s.py          # expect 0 warnings, 0 failures
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/api && kubectl -n fileapp rollout status deploy/web
```

### Verify

```bash
# not root any more
kubectl exec -n fileapp deploy/api -- id            # uid=10001 gid=10001
# root filesystem really is read-only
kubectl exec -n fileapp deploy/api -- sh -c 'touch /nope' 2>&1 | grep -qi "read-only" && echo "RO root OK"
# /data is writable by the new user (this is the one that can bite)
kubectl exec -n fileapp deploy/api -- sh -c 'touch /data/.wtest && rm /data/.wtest && echo "PVC writable OK"'
```

Then upload a file through https://files.local and confirm existing
files still list.

### If uploads break after the rollout

Pre-2.5 files on `api-files-pvc` are owned by root. `fsGroup: 10001`
makes kubelet chgrp the volume on mount, which normally fixes this. If a
file is still unreadable, fix ownership once from a root shell:

```bash
kubectl run fixperms -n fileapp --rm -it --restart=Never --image=busybox:1.36 \
  --overrides='{"spec":{"containers":[{"name":"fixperms","image":"busybox:1.36",
  "command":["sh","-c","chown -R 10001:10001 /data && ls -la /data"],
  "volumeMounts":[{"name":"f","mountPath":"/data"}],
  "securityContext":{"runAsUser":0}}],
  "volumes":[{"name":"f","persistentVolumeClaim":{"claimName":"api-files-pvc"}}]}}'
```

(The api Deployment uses `Recreate`, so scale it to 0 first - an RWO
volume cannot mount in two pods at once.)

### Rollback

`kubectl set image deploy/api api=192.168.1.210:5000/file-api:2.3 -n fileapp`
(and the same for web), then re-apply the v2.4 manifests to drop the
securityContext.

---

# v2.6 - Docker Hardened Images (distroless base)

Base image moves from `python:3.12-slim` (211 CVEs at time of writing) to
**Docker Hardened Images** `dhi.io/python:3.14-debian13` (0). DHI
Community is free - Apache 2.0 - and only needs `docker login dhi.io`
with a Docker ID. Images rebuild as **file-api:2.6 / file-web:2.6**.
Application code is unchanged.

## Dockerfile changes

| v2.5 (python:3.12-slim) | v2.6 (DHI) |
|---|---|
| `apt-get update && upgrade` in runtime stage | **removed** - no apt, and DHI ships patched |
| `groupadd`/`useradd` UID 10001 | **removed** - runtime variant is already nonroot **65532** |
| `python -m pip uninstall pip setuptools` | **removed** - the runtime variant ships no pip |
| `pip install --prefix=/install` -> `/usr/local` | **venv at `/opt/venv`** (`--copies`) |
| `HEALTHCHECK CMD python -c ...` (string form) | **exec form** - no shell to parse a string |
| tag only | **digest-pinned** (both stages) |

The venv matters: DHI puts python at `/usr/bin/python`, not
`/usr/local/bin/python`. The old `--prefix` copy would have produced a
silently broken image. The builder stage also now runs an import smoke
test (`import fastapi, psycopg2, bcrypt, ...`) so a bad dependency fails
at build time instead of at pod start.

Base digests (verified by pulling):

```
runtime: dhi.io/python:3.14-debian13@sha256:90dda111ab0b6667b9321d800e426120a9d4a39c73390825479caca4520e5fad
builder: dhi.io/python:3.14-debian13-dev@sha256:ef2d366cd461af05a717f2c245875d3cd24941d0727b0a34192ff097b5d6f807
```

Refresh with `docker pull <tag>` then
`docker inspect --format='{{index .RepoDigests 0}}' <tag>`.

Fallback: the working v2.5 Dockerfiles are kept as
`api/Dockerfile.python-slim` and `web/Dockerfile.python-slim`.

## Kubernetes change - REQUIRED

`runAsUser` / `runAsGroup` / `fsGroup` move **10001 -> 65532** in
`k8s/api.yaml` and `k8s/web.yaml`. fsGroup re-chowns the /data PVC on
mount, so existing uploads stay readable.

`scripts/validate_k8s.py` now also warns if a first-party pod is missing
`runAsNonRoot` or `runAsUser`.

## What you LOSE: the container shell

The runtime image is distroless. These stop working:

```bash
kubectl exec -n fileapp deploy/api -- id                    # no `id`
kubectl exec -n fileapp deploy/api -- sh -c 'touch /nope'   # no shell
kubectl exec -it -n fileapp deploy/api -- bash              # no bash
```

Python IS present, so most diagnostics survive - rewrite them:

```bash
# uid
kubectl exec -n fileapp deploy/api -- python -c "import os;print(os.getuid())"
# read-only root
kubectl exec -n fileapp deploy/api -- python -c "open('/nope','w')"     # expect PermissionError
# PVC writable (the check that matters)
kubectl exec -n fileapp deploy/api -- python -c "open('/data/.w','w').close();import os;os.remove('/data/.w');print('PVC writable OK')"
# DNS / connectivity (as used throughout this project)
kubectl exec -n fileapp deploy/api -- python -c "import socket;print(socket.gethostbyname('postgres.fileapp.svc.cluster.local'))"
```

For a real shell, attach an ephemeral container instead:

```bash
kubectl debug -n fileapp -it deploy/api --image=busybox:1.36 --target=api
```

## Rolling out

```bash
docker login dhi.io
./scripts/build-test.sh                  # builds + proves both images locally
docker push 192.168.1.210:5000/file-api:2.6
docker push 192.168.1.210:5000/file-web:2.6

python3 scripts/validate_k8s.py          # 0 warnings, 0 failures
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/api && kubectl -n fileapp rollout status deploy/web
```

Then log in at https://files.local, confirm existing files list, upload
one, and run the forgot-password flow.

## Honest notes

- "0 vulnerabilities" is as of scan time. DHI accrues CVEs too - repull
  and rebuild periodically, and refresh the pinned digests when you do.
- The 211 CVEs on python:3.12-slim were mostly in Debian packages the
  app never invokes, and `apt-get upgrade -y` already patched the
  fixable ones. The real win here is surface area: no shell means an
  attacker with RCE has nothing to pivot with.
- Python 3.12 -> 3.14.7. Your dependency set already runs on 3.14 (the
  local venv used for `pytest` is 3.14.4), and the builder import test
  guards it.
- Alpine variants are musl; `psycopg2-binary` ships no musl wheels, so
  stay on the debian13 variant.

## Rollback

```bash
kubectl set image deploy/api api=192.168.1.210:5000/file-api:2.5 -n fileapp
kubectl set image deploy/web web=192.168.1.210:5000/file-web:2.5 -n fileapp
```
Then re-apply the v2.5 manifests (UID 10001). Or rebuild from
`Dockerfile.python-slim` if you want to leave DHI entirely.

## v2.6.1 - pip regression fix

Field report: `docker run --rm --entrypoint python <img> -m pip --version`
returned `pip 26.2.1 from /opt/venv/lib/python3.14/site-packages/pip`.

Cause: `python -m venv` bootstraps pip **into the venv**, and the runtime
stage copies `/opt/venv` wholesale. v2.5's `--prefix=/install` tree never
contained pip, so its `pip uninstall` line was enough; switching to a
venv in v2.6 reintroduced pip while that line had been dropped as
"no longer needed".

Fix (both Dockerfiles, builder stage): after installing requirements,
`pip uninstall -y pip` plus `rm -rf` of pip/setuptools/wheel/pkg_resources
leftovers, then two build-time assertions - imports still succeed after
the strip, and `python -m pip` is unreachable (`! ... >/dev/null 2>&1`,
so the build FAILS if pip survives).

Images rebuild as **file-api:2.6.1 / file-web:2.6.1**.
`scripts/build-test.sh` already asserted this; it would have caught the
same thing at step 2.

## v2.6.2 - build-test gate actually gates (script only, no image rebuild)

Field report: a v2.6.1 run printed a stray `curl: (22) The requested URL
returned error: 401` and then `BUILD TEST PASSED`.

Two bugs, both in `scripts/build-test.sh`:

1. The anonymous-request check used `curl -fsS`. `-f` makes curl exit 22
   on a 4xx and print nothing to stdout - but a 401 is exactly what that
   check WANTS. So it never matched, and the check silently did nothing.
2. Worse, every check used the shape `cmd && echo "... OK"`. Under
   `set -e`, a failing command on the left of an `&&` list does **not**
   exit the script (POSIX: -e is ignored for any command in an AND-OR
   list other than the last). So ANY failing check printed nothing and
   the script still reported PASSED - a gate that did not gate.

Fix: `ok()` / `fail()` helpers, with `fail()` exiting non-zero, used by
every check; and the anonymous check now captures the status code
without `-f` and asserts it is 401 or 403.

Images are unchanged - still **file-api:2.6.1 / file-web:2.6.1**. Only
re-run the script:

```bash
chmod +x scripts/*.sh      # zip extraction can drop the exec bit
./scripts/build-test.sh
```

You should now see `anonymous request rejected (401)  OK` where the
stray curl error used to be.

---

# v2.7 - security response headers

Manifest/script only. **No image rebuild** - still file-api:2.6.1 /
file-web:2.6.1.

New `k8s/ingress-nginx-headers.yaml`: a `custom-response-headers`
ConfigMap that ingress-nginx attaches to every response.

| Header | Value | What it stops |
|---|---|---|
| X-Content-Type-Options | nosniff | MIME-type guessing |
| X-Frame-Options | DENY | clickjacking (legacy browsers) |
| Referrer-Policy | no-referrer | leaking the URL - **a reset link contains a live token** |
| Permissions-Policy | camera=(), microphone=(), geolocation=(), payment=(), usb=() | unused browser features |
| X-XSS-Protection | 0 | the deprecated auditor, which can itself introduce bugs |
| Cross-Origin-Opener-Policy | same-origin | cross-origin window tampering |
| Content-Security-Policy | see below | XSS, injected scripts, framing |

```
default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; form-action 'self'; frame-ancestors 'none';
base-uri 'self'; object-src 'none'
```

`script-src 'none'` is safe because the app ships **no JavaScript**.
`style-src` needs `'unsafe-inline'` for the `<style>` block in web.py's
HEAD template. **If you ever add a `<script>` tag, CSP will block it** -
that is intended; loosen it deliberately.

Why a ConfigMap and not a `configuration-snippet` annotation:
ingress-nginx disables snippet annotations by default (since 1.9) as a
hardening measure, and re-enabling them opens that door for every
Ingress in the cluster. `add-headers` needs no such exception.

## Apply

```bash
kubectl apply -f k8s/ingress-nginx-headers.yaml

kubectl -n ingress-nginx patch configmap ingress-nginx-controller \
  --type merge -p '{"data":{
    "add-headers":"ingress-nginx/custom-response-headers",
    "hsts":"true","hsts-max-age":"31536000",
    "hsts-include-subdomains":"true","hsts-preload":"false"}}'

kubectl -n ingress-nginx rollout restart deploy/ingress-nginx-controller
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller
```

## Verify - on the wire, not in a browser UI

```bash
./scripts/check-headers.sh
```

Asserts every header above, the HTTP->HTTPS redirect, and that the
certificate chain verifies against `lab-ca.crt`. Exits non-zero on any
failure. Then re-test the app: login, upload, and the forgot-password
flow (CSP `form-action 'self'` covers those forms).

## Note on the HSTS browser display

Firefox's Security panel may keep showing
`HTTP Strict Transport Security: "Disabled"` for `files.local` even
though the header is present on the wire. Confirm with
`curl -sSI --cacert lab-ca.crt https://files.local/login | grep -i strict`.
Browsers are known to override HPKP for user-installed root CAs, and may
apply similar handling to HSTS state for privately-issued certificates -
this was not confirmed. The protection HSTS provides is already covered
here by the ingress 308 redirect and the `Secure` session cookie.

## v2.7.1 - CSP upgrade-insecure-requests

Added `upgrade-insecure-requests` to the Content-Security-Policy in
`k8s/ingress-nginx-headers.yaml`. Full policy now:

```
default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; form-action 'self'; frame-ancestors 'none';
base-uri 'self'; object-src 'none'; upgrade-insecure-requests
```

It rewrites any `http://` subresource or same-origin navigation to
`https://` **before** the request leaves the browser. The app loads no
external assets, so nothing changes today - it is insurance: a future
`http://` asset gets upgraded rather than blocked as mixed content.
Cross-origin top-level navigations are unaffected; that is HSTS and the
browser's own HTTPS-First behaviour.

`scripts/check-headers.sh` now asserts three CSP directives
individually (`script-src 'none'`, `frame-ancestors 'none'`,
`upgrade-insecure-requests`) rather than only checking that a CSP header
exists.

Still manifests/scripts only - images remain file-api:2.6.1 /
file-web:2.6.1.

## v2.7.2 - scripts/check-app.sh

Scripts and docs only. Images stay file-api:2.6.1 / file-web:2.6.1.

New `scripts/check-app.sh` - in-cluster application checks, the layer
below `check-headers.sh`:

1. every pod Running and every Deployment at full readyReplicas
2. api / web / postgres / mailpit-smtp Services have endpoints
3. from a **web** pod: resolve `api.fileapp.svc.cluster.local`, call
   `/ready`, and assert DNS < 1s and the call < 2s (a regression guard
   for the ndots search-list stall that once cost 4s per lookup)
4. **/data and EVERY per-user directory** - writes and removes a probe
   file in each, and audits the whole tree for paths owned by another uid
5. schema: users / files / password_resets, the
   `files_user_filename_uq` index, `users.token_version`, row counts,
   and any orphaned pre-v2 file rows

Read-only except for the probe files, which are removed immediately.
All in-pod work goes through `python -c` because the runtime image is
distroless (no shell).

### Why check 4 exists

`fsGroup` fixes the ownership of the volume ROOT. Pre-existing
subdirectories created under a different UID keep their old ownership.
After the 10001 -> 65532 move, `/data` was `0777` and writable while
`/data/1` was still `root:root 0755`, so every upload failed with
`PermissionError: '/data/1/<file>.part'` **while the documented check
(`touch /data/.w`) passed**. The runbook check in v2.5/v2.6 tested the
mount point, not the directory files actually land in. This script tests
the real path. Run it after any change to runAsUser, runAsGroup or
fsGroup.

---

# v2.8 - per-token revocation (JWT denylist)

Closes the last authentication gap: **logout now actually revokes the
token**, and only that one. Images rebuild as **file-api:2.8 /
file-web:2.8** (app code changed - a rebuild IS required this time).

## The gap this closes

Before v2.8, `/logout` only cleared the Flask session cookie. The JWT
itself stayed valid until `exp` - up to 60 minutes. Anyone holding a
copy of that token (shared machine, proxy log, captured header) could
keep using it after the user logged out.

`token_version` already existed but is all-or-nothing: bumping it kills
**every** session for that user. Right for a password reset, wrong for
"log out this laptop".

## Design

- Every token now carries a `jti` claim - 16 random bytes, unique per
  token (`secrets.token_urlsafe`).
- New `revoked_tokens (jti, user_id, expires_at, revoked_at)` table.
  `POST /auth/logout` inserts the jti; the web `/logout` calls it before
  clearing the cookie (best effort - if the API is down the cookie is
  still dropped and the token expires on its own).
- **The check costs nothing extra.** `current_user()` already hit the DB
  once per request for `token_version`; the denylist folds into that
  same statement:

  ```sql
  SELECT u.token_version,
         EXISTS (SELECT 1 FROM revoked_tokens r WHERE r.jti = %s)
  FROM users u WHERE u.id = %s
  ```

  One round trip, two revocation mechanisms.
- **The table is self-pruning.** A revoked jti only matters until the
  token would have expired anyway, so `expires_at` bounds it: each
  logout first runs `DELETE FROM revoked_tokens WHERE expires_at < now()`.
  Steady-state size is "tokens revoked in the last TOKEN_TTL_MINUTES",
  not "every logout ever".
- Only the **jti** is stored, never the token. A leaked denylist row is
  not a usable credential.
- Pre-v2.8 tokens have no `jti`; they still validate (`jti` defaults to
  `""`) and simply cannot be revoked individually - they expire within
  the TTL. No forced logout on upgrade.

Which mechanism fires when:

| Event | Mechanism | Scope |
|---|---|---|
| Logout | `revoked_tokens` (jti) | that one session |
| Password reset | `token_version` bump | every session for that user |
| Token expiry | `exp` claim, verified by PyJWT | self-enforcing, no state |

## Tests

`tests/test_api.py` gains five: logout revokes only that session (the
other device keeps working), logout is idempotent, a revoked token
cannot upload, logout rejects a garbage token, and the denylist stores
the jti rather than the token. `tests/test_security.py` gains four
covering jti uniqueness, `exp` exposure, and legacy-token decoding.

`scripts/build-test.sh` proves it against a real Postgres: two logins ->
distinct tokens -> logout one -> that token 401s while the other still
returns 200.

## Rolling out

```bash
docker build -t 192.168.1.210:5000/file-api:2.8 api && docker push 192.168.1.210:5000/file-api:2.8
docker build -t 192.168.1.210:5000/file-web:2.8 web && docker push 192.168.1.210:5000/file-web:2.8
./scripts/build-test.sh
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/api && kubectl -n fileapp rollout status deploy/web
./scripts/check-app.sh
```

The `revoked_tokens` table is created by the startup migration - nothing
manual. Existing sessions keep working.

### Verify by hand

Log in from two browsers (or one normal + one private window). Log out
of the first. The first is bounced to the login page; **the second keeps
working**. Then in Loki:

```logql
{namespace="fileapp", container="api"} |~ "logout for"
```

## v2.8.1 - check-app.sh could report PASSED while blind (scripts only)

Field report: `sudo ./scripts/check-app.sh` printed
`all pods Running/Completed  OK` **and** a wall of
`couldn't get current server API group list ... localhost:8080`.

Two bugs, one cause.

**Cause:** `sudo` resets `$HOME` to `/root`, so kubectl looked for
`/root/.kube/config`, found none, and fell back to its default
`localhost:8080`. The script needs no root at all - the only reason
sudo was used is the exec-bit problem below.

**Bug 1 - false passes.** With kubectl broken, every query returned
empty and stderr was suppressed, so "no unready pods" was
indistinguishable from "I cannot see any pods". Same on the denylist
stale-row check. Fixed: a `kubectl cluster-info` + namespace preflight
that exits 2 with a targeted message (it detects `$SUDO_USER` and names
sudo as the likely cause), an explicit empty-list check on the pod
query, a `SELECT 1` probe before the schema checks, and empty-result
handling on the stale-row check. `restore-drill.sh` got the same
preflight.

**Bug 2 - the exec bit.** Scripts shipped as `-rw-r--r--` in every
bundle. The build directory used for packaging strips the executable
bit and will not accept `chmod +x`, so the zip recorded the stripped
mode. Fixed by building the archive where permissions survive. If you
still land on a bundle without it:

```bash
chmod +x scripts/*.sh deploy.sh
```

Scripts only - images remain file-api:2.8 / file-web:2.8.

---

# v2.9 - download and delete

Users can now download and delete their own files. Images rebuild as
**file-api:2.9 / file-web:2.9**. No schema change - no migration.

## API

| Route | Behaviour |
|---|---|
| `GET /files/{filename}` | streams the caller's own file back |
| `DELETE /files/{filename}` | removes the row and the blob |

## Three deliberate security decisions

**1. The database decides ownership, not the path.** Both routes look
the file up as `(user_id, filename)`. Even if a crafted name survived
`sanitize_filename()`, the query is scoped to the caller's own id, so
another user's directory is unreachable. A file belonging to someone
else returns **404, not 403** - 403 would confirm it exists.

**2. Downloads are `application/octet-stream` + `Content-Disposition:
attachment`, never the guessed MIME type.** If a user uploads
`evil.html` or `evil.svg` and the app served it with its real type, the
browser would render it **in the app's own origin** - stored XSS with
access to the session cookie. Forcing a download makes uploaded content
inert. This pairs with the `X-Content-Type-Options: nosniff` header from
v2.7, which stops the browser second-guessing us.

**3. Deleting is a POST behind a confirmation page.** The Delete link is
a **GET** that only renders "Delete this file?" - it has no side effect,
so a prefetcher, crawler or accidental click cannot destroy anything.
The destructive step is a **POST**, which the `SameSite=Lax` session
cookie blocks cross-site (CSRF). There is no JS confirm dialog because
the v2.7 CSP sets `script-src 'none'`; a server-rendered page is both
stricter and more reliable.

## Ordering: row first, blob second, one transaction

```python
DELETE FROM files ... RETURNING id     # 404 if it returns nothing
os.remove(path)                        # still inside the transaction
```

If the unlink raises, `db_conn()` rolls back and the row survives - so
the listing never shows a file whose bytes are already gone. The reverse
order would strand a row pointing at nothing. A crash between unlink and
commit can still leave an orphaned blob; that is invisible and wastes
only disk, which is the better failure.

## Streaming

The web pod proxies downloads with `stream=True` and 64 KB chunks. With
`MAX_UPLOAD_MB=100`, buffering would mean 100 MB of RAM per concurrent
download in a pod limited to 256 Mi.

## Tests

17 new: 10 API (content round trip, octet-stream + attachment headers,
another user's file 404s, missing file 404s, unauthenticated rejected,
path traversal blocked, delete removes BOTH row and blob, cannot delete
another user's file, revoked token cannot delete) and 7 web (links
render, GET /delete never calls the API, POST does, errors surface,
login required). `build-test.sh` adds a live round trip against real
Postgres: download content, content-type, 404, delete, 404 again, gone
from the listing.

## Rolling out

```bash
docker build -t 192.168.1.210:5000/file-api:2.9 api && docker push 192.168.1.210:5000/file-api:2.9
docker build -t 192.168.1.210:5000/file-web:2.9 web && docker push 192.168.1.210:5000/file-web:2.9
./scripts/build-test.sh
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/api && kubectl -n fileapp rollout status deploy/web
./scripts/check-app.sh
```

Then at https://files.local: download a file (it should save, not open
in a tab), click Delete, confirm the page appears, cancel, then delete
for real. Watch it in Loki:

```logql
{namespace="fileapp", container="api"} |~ "download |deleted "
```

## Known gaps

- No trash/undo - delete is permanent and the file blobs still have no
  off-cluster backup. That remains the top open item.
- No per-file share links; downloads are always authenticated.

---

# v2.10 - file-blob backups + DB/disk consistency check

Manifests and scripts only. Images stay **file-api:2.9 / file-web:2.9**.

## Closing the backup gap

`pg_dump` only ever saved users and file **metadata**. The uploaded bytes
on `api-files-pvc` had no backup, so a lost volume meant a restored
database full of rows pointing at nothing. With v2.9 deletion now
permanent, that gap mattered more.

New `k8s/files-backup.yaml`: a `files-backup-pvc` plus a nightly CronJob
that tars `/data`, verifies the archive with `tar -tzf`, and prunes past
`RETENTION_DAYS` (7) - the same shape as the pg_dump job.

### Two design points forced by ReadWriteOnce

`api-files-pvc` is RWO, so it can only be mounted from the node that
already holds it. The job therefore uses **required podAffinity onto the
api pod's node** - the exact opposite of the postgres backup, which uses
anti-affinity to keep dumps off the database's node.

The honest consequence: **the blob archive lives on the same node as the
blobs it protects.** It defends against accidental deletion, a bad
migration, and DB/blob divergence. It does **not** defend against that
node's disk failing. Only the off-cluster copy does. (The real fix is
ReadWriteMany storage or object storage, which would also let the api
Deployment scale past one replica - see Known gaps.)

The source is mounted `readOnly: true`; a backup job must never be able
to mutate what it is backing up.

### Ordering: 03:00 DB, 03:30 blobs

Deliberate. A file uploaded between the two ends up **in the archive but
not the dump** - an invisible orphan blob. The reverse order would put it
**in the dump but not the archive** - a row whose bytes are missing,
which is the failure that produces
`WARNING: <file> is recorded for user N but missing on disk`. Same
reasoning as the delete ordering in v2.9: prefer the harmless failure.

## Restoring blobs

```bash
kubectl scale deploy/api -n fileapp --replicas=0     # frees the RWO volume
kubectl wait --for=delete pod -n fileapp -l app=api --timeout=120s
kubectl apply -f k8s/files-restore-shell.yaml
kubectl exec -it -n fileapp files-restore-shell -- bash

  ls -lh /backups
  tar -tzf /backups/files-<STAMP>.tar.gz | head      # inspect first
  tar -xzf /backups/files-<STAMP>.tar.gz -C /data    # restore in place
  chown -R 65532:65532 /data                         # match runAsUser
  exit

kubectl delete pod -n fileapp files-restore-shell
kubectl scale deploy/api -n fileapp --replicas=1
./scripts/check-app.sh                               # section 6 reconciles
```

After a **paired** restore (DB dump + blob archive taken 30 min apart),
expect check 6 to report a small number of "blobs with no row" - files
uploaded between the two jobs. They are invisible to users and safe to
delete.

## Off-cluster copy - now TWO volumes

```bash
kubectl apply -f k8s/backup-shell.yaml
kubectl cp fileapp/backup-shell:/backups ./pg-backups-$(date +%Y%m%d)
kubectl delete pod -n fileapp backup-shell

kubectl apply -f k8s/files-restore-shell.yaml
kubectl cp fileapp/files-restore-shell:/backups ./file-backups-$(date +%Y%m%d)
kubectl delete pod -n fileapp files-restore-shell
```

(The second helper needs `api` scaled to 0 only for a **restore**; a
read-only copy out can run alongside it, since both pods land on the
same node and RWO permits multiple pods per node.)

## check-app.sh: new section 6

Reconciles the database against the disk, comparing `user_id/filename`
rows to actual paths:

- **rows with no blob** - a download would 404 (this is what the
  `bob-file.txt` WARNINGs were)
- **blobs with no row** - invisible, wasting disk
- **files stranded at /data root** - the pre-v2 `client__filename`
  layout, which is how two files survived a delete that only removed
  their rows

Plus a Backups section asserting both CronJobs have a recorded
`lastSuccessfulTime`.

## Apply

```bash
kubectl apply -f k8s/files-backup.yaml
kubectl get cronjob -n fileapp

# don't wait until 03:30 - trigger one now
kubectl create job -n fileapp --from=cronjob/files-backup manual-files-$(date +%s)
kubectl logs -n fileapp -l app=files-backup --tail=20
./scripts/check-app.sh
```

Expect `OK: /backups/files-<stamp>.tar.gz (<size>, N entries)`.

**If the job stays Pending**, the api pod has moved to a node where
`files-backup-pvc` does not exist. Check with
`kubectl get pvc -n fileapp` and `kubectl get pods -n fileapp -o wide`;
the fix is to delete the (empty) PVC and let it re-provision on the
current node.

## Known gaps

- Blob archives sit on the same node as the blobs. Off-cluster copy is
  the only protection against disk loss - automate it.
- RWO forces both the api Deployment to one replica and these backups
  onto one node. ReadWriteMany (NFS) or object storage (MinIO/S3) fixes
  both at once and is the natural next chapter.
- No trash/undo on delete.

---

# v2.11 - ReadWriteMany storage (NFS)

Manifests and scripts only. Images stay **file-api:2.9 / file-web:2.9**.

RWO was the single constraint behind three separate limitations. RWX
removes all three at once:

| Was | Now |
|---|---|
| api limited to **1 replica** | **2 replicas**, spread across nodes |
| `strategy: Recreate` (downtime on every deploy) | `RollingUpdate`, maxUnavailable 0 - **zero-downtime deploys** |
| files-backup **pinned** to the api node | schedules anywhere |

## Prerequisites - host side (a manifest cannot do this)

**1. On EVERY node** (master, worker1, worker2) - the kubelet mounts NFS
in the node's kernel namespace, so the client tooling must exist there:

```bash
sudo apt-get update && sudo apt-get install -y nfs-common
```

**2. On the master only** - the NFS server and export:

```bash
sudo apt-get install -y nfs-kernel-server
sudo mkdir -p /srv/nfs/fileapp
sudo chown 65532:65532 /srv/nfs/fileapp
echo '/srv/nfs/fileapp 192.168.190.0/24(rw,sync,no_subtree_check,no_root_squash)' \
  | sudo tee -a /etc/exports
sudo exportfs -ra
sudo systemctl enable --now nfs-server
sudo exportfs -v          # should list the export
```

`no_root_squash` is needed so the CSI driver can create per-PVC
subdirectories. The export is limited to the VM subnet.

Note the client address is the **node** IP (192.168.190.x), not a pod
IP - which is why this works at all after the pod-CIDR migration.

**3. csi-driver-nfs** (check the releases page for the current version):

```bash
helm repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
helm repo update
helm install csi-driver-nfs csi-driver-nfs/csi-driver-nfs \
  --namespace kube-system --version v4.11.0
kubectl -n kube-system get pods -l app.kubernetes.io/name=csi-driver-nfs
```

## POSTGRES STAYS ON RWO - do not "convert" it

Running PostgreSQL on NFS is a known way to corrupt a database: file
locking and fsync semantics differ from a local filesystem. `postgres`
keeps its local-path RWO volume and stays at one replica. RWX is for
the file blobs only. `postgres-backup-pvc` also stays as it is - it
works, and its job already has its own anti-affinity design.

## Migration - step by step

```bash
# 1. Storage class and the new claims
kubectl apply -f k8s/nfs-storageclass.yaml
kubectl apply -f k8s/api-files-rwx.yaml
kubectl get pvc -n fileapp          # api-files-rwx + files-backup-rwx Bound

# 2. Stop writers (api-files-pvc is RWO - the Job cannot mount it otherwise)
kubectl scale deploy/api -n fileapp --replicas=0
kubectl wait --for=delete pod -n fileapp -l app=api --timeout=120s

# 3. Copy, with verification
kubectl apply -f k8s/migrate-files-to-rwx.yaml
kubectl logs -n fileapp -l job-name=migrate-files-to-rwx -f
```

The Job compares file lists **and md5 checksums** and exits non-zero on
any mismatch. Wait for `MIGRATION OK: N source / N destination files,
checksums match` before continuing.

```bash
# 4. Switch the app over (also brings 2 replicas + RollingUpdate)
kubectl apply -f k8s/api.yaml
kubectl -n fileapp rollout status deploy/api --timeout=180s
kubectl apply -f k8s/files-backup.yaml

# 5. Verify
./scripts/check-app.sh
kubectl delete job -n fileapp migrate-files-to-rwx
```

The old RWO volume is untouched and still defined in
`k8s/api-files-pvc-legacy.yaml`. Keep it until you are confident, then:

```bash
kubectl delete -f k8s/api-files-pvc-legacy.yaml
```

## What check-app.sh now proves

New section 7 asserts `api-files-rwx` really is `ReadWriteMany`, reports
replica count and how many distinct nodes they occupy, and runs the test
that actually matters: **pod A writes a probe file, pod B must see it.**
Two pods each mounting their own private volume would pass every other
check and fail this one.

## Honest limits

- The NFS server is the master node. It is now a single point of failure
  for uploads: if the master is down, `/data` is unavailable to both api
  replicas. This trades "one node's disk" for "one node's service" - and
  buys multi-replica plus rolling deploys. Real HA needs replicated
  storage (Longhorn, Ceph) or object storage (MinIO/S3).
- Archives and live data still share the NFS server, so the off-cluster
  copy remains the only defence against losing it. Automate it.
- NFS `hard` mounts mean a server outage hangs I/O rather than erroring.
  That is the right default for data integrity, but a pod may sit
  blocked instead of failing fast.

## v2.11.1 - backup freshness + locale-stable reconciliation (scripts only)

Field report: after v2.11, `check-app.sh` reported
`cronjob/files-backup  FAIL  no successful run recorded yet` and printed
`comm: file 1 is not in sorted order` in section 6.

**Bug 1 - "never run" reported as failure.** A CronJob created minutes
ago has no `lastSuccessfulTime`, which is correct, not broken. The check
now reads `lastScheduleTime` as well and separates three states:

| State | Verdict |
|---|---|
| never scheduled, never succeeded | note: "created, not scheduled yet - trigger one to verify" |
| scheduled but never succeeded | FAIL |
| succeeded, within `MAX_BACKUP_AGE_DAYS` (2) | OK |
| succeeded, older than that | **FAIL - BACKUP IS STALE** |

**Bug 2 - the check was too lenient, and hid a real problem.** The old
version passed on the mere presence of a `lastSuccessfulTime`. In the
same run that produced the false failure it printed
`cronjob/postgres-backup last success 2026-08-21T16:52:15Z  OK` - a
**14-day-old** backup, from a job scheduled to run nightly, marked OK. A
backup check that cannot detect a stale backup is the exact failure mode
backups are supposed to guard against. Freshness is now asserted.

**Why nightly jobs go stale here:** the schedule is `0 3 * * *` and
`30 3 * * *`. A lab cluster that is powered off overnight never reaches
those times, and Kubernetes does not run missed schedules retroactively.
Options, in order of preference:

1. Move both schedules into hours the cluster is actually up - one line
   in `k8s/postgres-backup.yaml` and `k8s/files-backup.yaml`, e.g.
   `schedule: "0 14 * * *"`.
2. Trigger manually and rely on `check-app.sh` to flag staleness:
   `kubectl create job -n fileapp --from=cronjob/postgres-backup manual-$(date +%s)`

Also worth knowing: if a CronJob misses more than 100 consecutive
schedules, the controller stops scheduling it **permanently** and logs
"too many missed start time". At one missed run per day that is ~100
days of downtime; recreating the CronJob clears it.

**Bug 3 - locale-dependent reconciliation.** Section 6 sorted with the
shell's locale (case-insensitive under UTF-8: `hi.txt` before `Ho.txt`)
but `comm` compares bytes, so the two disagreed and `comm` warned. The
results happened to be right with three files; with more they could
have been wrong - a genuinely missing blob could be paired away. All
sorts and the `comm` calls now run under `LC_ALL=C`.

---

# v2.12a - professional frontend, still zero JavaScript

Presentation only. The API is untouched; `web/web.py` gets a new template
layer. Images rebuild as **file-api:2.12 / file-web:2.12** (the api tag
moves only to keep the pair in step - its code did not change).

## The constraint is the design brief

The v2.7 CSP sets `script-src 'none'`, so a `<script>` tag would be
blocked by the browser, not merely discouraged. `default-src 'self'`
also rules out a CDN stylesheet or webfont. Everything below is plain
CSS and semantic HTML:

- **Design tokens** as CSS custom properties (colour, radius, shadow).
- **Automatic dark mode** via `prefers-color-scheme` - no toggle, no JS,
  no stored preference.
- **System font stack** - nothing fetched from a CDN, so nothing leaks
  which pages a user visits, and there is no render-blocking font.
- **Responsive** through a viewport meta tag and one media query; the
  file table scrolls horizontally rather than breaking.
- **Focus-visible rings, hover states**, styled
  `::file-selector-button` - the file input is normally the ugliest
  control on a page and is stylable without script.
- **Inline SVG icons** as markup, plus an **SVG favicon as a `data:`
  URI** (allowed by `img-src 'self' data:`). That also removes the
  `/favicon.ico` 404 every page load used to log.
- **Accessibility**: `<label>`-wrapped fields, `autocomplete` hints so
  password managers work, `role="alert"` on flash messages,
  `aria-hidden` on decorative icons, real `<thead>`/`<tbody>`.

Pages: header with brand and account chip, cards, an empty state, a
distinct destructive style on the delete confirmation, and per-page
`<title>`s.

## Server-side formatting

`human_size()` and `human_date()` turn `2411724` into `2.3 MB` and an
ISO timestamp into `05 Sep 2026, 13:21`. Formatting happens in the web
tier via `decorate()`; the API keeps returning raw values, so its
contract is unchanged.

## Two implementation notes

- `{% block %}` needs template inheritance, which
  `render_template_string` has no loader for - it raised
  "block 'title' defined twice". Titles use `{% set %}` before the head
  instead.
- Title text passes through `{{ }}` and is autoescaped, so `&middot;`
  rendered literally as `&amp;middot;`. Separators are plain ASCII.

## Tests

Four new, including a **permanent guard**: every page is rendered and
asserted to contain no `<script`, no `javascript:` URL and no inline
`on*=` handler. If anyone reaches for JS later, the suite fails before
the CSP does. Also: well-formed markup with one `<body>`/`</html>` and a
viewport tag, humanised sizes and dates, and the empty state. One
existing test asserted `name=email` on unquoted markup and now asserts
`name="email"` - the attributes are properly quoted.

87 tests total.

## Rolling out

```bash
docker build -t 192.168.1.210:5000/file-web:2.12 web && docker push 192.168.1.210:5000/file-web:2.12
docker build -t 192.168.1.210:5000/file-api:2.12 api && docker push 192.168.1.210:5000/file-api:2.12
./scripts/build-test.sh
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/web --timeout=180s
./scripts/check-app.sh
```

RollingUpdate means no downtime. Then load https://files.local, check the
browser Console for CSP violations (there should be none), and try the
OS dark/light setting to see the theme follow it.

---

# v2.12b - external stylesheet, and the CSP loses 'unsafe-inline'

Images rebuild as **file-api:2.12 / file-web:2.12** (only the web image
actually changed; both are tagged together so build-test stays coherent).

## What changed

The redesigned frontend delivered its CSS in an inline `<style>` block,
which forced the Content-Security-Policy to keep
`style-src 'self' 'unsafe-inline'`. `'unsafe-inline'` does not only
permit *our* inline styles - it permits **any** injected `<style>` tag,
which is the one directive that meaningfully weakened the policy.

The stylesheet now comes from a same-origin route:

```
GET /style.css        text/css, ETag, Cache-Control: public, max-age=3600
<link rel="stylesheet" href="/style.css?v=<hash>">
```

The `?v=` is the first 12 hex of a SHA-256 of the stylesheet, so a deploy
busts the browser cache automatically, and the same hash is the ETag so
repeat visits get a 304 instead of the body.

CSP is now:

```
default-src 'self'; script-src 'none'; style-src 'self';
img-src 'self' data:; form-action 'self'; frame-ancestors 'none';
base-uri 'self'; object-src 'none'; upgrade-insecure-requests
```

Both `script-src 'none'` **and** `style-src 'self'` with no inline
escape hatch. There is no JavaScript anywhere in the app, no CDN, and no
external font - the favicon is an inline SVG data URL.

## ORDER MATTERS when applying

Deploy the app **first**, then tighten the header. The new app works
under the old policy (`'self'` was already allowed), but the old app
would render unstyled under the new one.

```bash
# 1. app first
docker build -t 192.168.1.210:5000/file-api:2.12 api && docker push 192.168.1.210:5000/file-api:2.12
docker build -t 192.168.1.210:5000/file-web:2.12 web && docker push 192.168.1.210:5000/file-web:2.12
./scripts/build-test.sh
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/web --timeout=180s

# 2. then the stricter CSP
kubectl apply -f k8s/ingress-nginx-headers.yaml
kubectl -n ingress-nginx rollout restart deploy/ingress-nginx-controller
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller

# 3. verify
./scripts/check-headers.sh
./scripts/check-app.sh
```

## Tests

Four new web tests: `/style.css` serves `text/css` with an ETag, a
matching `If-None-Match` returns 304, every page links the stylesheet
and contains **no** `<style>` block, and no rendered page (including the
CSS itself) contains `<script`, `javascript:`, or any `on*=` handler.

`check-headers.sh` gains an **absence** assertion - it now fails if
`unsafe-inline` appears anywhere in the CSP. Presence checks alone
cannot catch a directive that undoes the policy.

## Note on "no CSS"

CSS cannot execute code; the attacks associated with it (attribute-
selector exfiltration, overlay tricks) all require an attacker to
INJECT CSS, which needs an XSS that `script-src 'none'` plus Jinja2
autoescaping already prevents. Removing CSS would not have improved
security - it would only have removed the styling. Moving it out of the
inline block and dropping `'unsafe-inline'` is the change that actually
hardens the policy.

---

# v2.12.1 - the CSS guarantees become enforceable

v2.12 made the stylesheet inert and dropped `'unsafe-inline'`. v2.12.1
makes both facts **testable**, and closes the remaining CSS channels at
the CSP layer. Manifests and tests only - **no image rebuild**.

## CSP: name the channels instead of inheriting them

Unstated directives silently fall back to `default-src 'self'`, which
still permits same-origin fetches. Now stated explicitly:

```
font-src 'none'; connect-src 'none'; media-src 'none';
frame-src 'none'; worker-src 'none'; manifest-src 'none'
```

`font-src 'none'` is the one that matters for this threat model. The
CSS text-exfiltration trick is `@font-face` with `unicode-range`: the
attacker declares one font per glyph range, and the browser reveals
which characters a page contains by which font files it requests. The
app uses system fonts only - nothing is ever fetched - so blocking the
whole channel costs nothing and removes it permanently.

`connect-src 'none'` blocks fetch/XHR/WebSocket/EventSource/Beacon.
With `script-src 'none'` nothing can issue them anyway; a policy is
cheapest when it states intent rather than implying it.

## Tests that keep it true

The stylesheet is clean **today**. Without a guard, one convenient
`background-image` in six months silently reopens the exfiltration
channel. Six new tests in `tests/test_web.py` assert the served CSS
contains no `url(`, no `@import`, no `@font-face`, no absolute URL, no
`expression(`, and that no rendered page carries a `style=` attribute.

Six more in `tests/test_security.py` read `k8s/ingress-nginx-headers.yaml`
directly and assert the shipped CSP still has `style-src 'self'` with no
`unsafe-inline`, no `unsafe-eval`, `script-src 'none'`, `font-src 'none'`,
`connect-src 'none'`, `frame-ancestors 'none'`, `base-uri 'self'` and
`form-action 'self'`.

Both guards were verified by deliberately breaking them: injecting
`url("https://evil.example/x.png")` into the stylesheet failed
`test_css_has_no_network_requests` and `test_css_has_no_absolute_urls`;
adding `'unsafe-inline'` back to style-src failed
`test_style_src_is_self_without_unsafe_inline`. A guard that has never
been seen to fail is not a guard.

103 tests total.

## Applying

Only the header ConfigMap changes:

```bash
kubectl apply -f k8s/ingress-nginx-headers.yaml
kubectl -n ingress-nginx rollout restart deploy/ingress-nginx-controller
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller
./scripts/check-headers.sh
```

The v2.12 ordering rule still stands: **the web image must be deployed
before the tightened CSP.** An older image with an inline `<style>` block
renders unstyled under `style-src 'self'`.

## What this does NOT defend against

- A malicious **first-party** stylesheet. Everything here assumes our own
  CSS is trustworthy and stops an *injected* one; the tests are what keep
  the first-party file honest.
- Fingerprinting the browser already performs without our help (User-Agent,
  Accept-Language, TLS fingerprint). `prefers-color-scheme` is used but
  leaks nothing here **because no request differs by theme** - a
  fingerprinting signal needs to be observable by the server.

## v2.12.2 - check-headers.sh: skipped is not failed (scripts only)

Field report: `check-headers.sh` reported
`certificate chain  FAIL  verify result 20` after moving to a fresh
project directory.

**Cause:** `lab-ca.crt` was not in the new directory. The script falls
back to `curl -k` in that case and prints "skipping cert validation" -
but then still read `%{ssl_verify_result}`, which curl populates with
the real verification outcome **even in insecure mode**. Result 20 is
`X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY` - "no CA to check
against", which is exactly the situation the script had just announced
it was ignoring. It failed a check it had deliberately chosen not to run.

**Fix:** an explicit `HAVE_CA` flag. With a CA bundle the chain is
asserted; without one it is reported as **skipped**, not failed. Verify
codes are now decoded rather than printed raw:

| code | meaning |
|---|---|
| 0 | chain verifies |
| 10 | certificate expired |
| 18 | self-signed and not in the bundle |
| 20 | issuer not found - wrong CA file for this cert |
| 99 | could not connect at all |

All four branches were exercised before shipping.

**To restore the real assertion**, put the CA back in the project
directory:

```bash
kubectl get secret -n cert-manager lab-ca-secret \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > lab-ca.crt
./scripts/check-headers.sh
```

Worth adding `lab-ca.crt` to the copy list whenever you move to a new
project directory - it is gitignored deliberately, and it is what turns
"the headers are present" into "the headers are present **and** the
chain is trusted".

## v2.12.2 - check-headers.sh no longer fails a check it skipped

Field report: a fresh checkout with no `lab-ca.crt` printed
`no lab-ca.crt found, skipping cert validation (-k)` and then
`certificate chain  FAIL  verify result 20`.

The two statements contradicted each other. With `-k`, curl skips
*enforcement* but still performs verification and reports the outcome in
`%{ssl_verify_result}`; the old code asserted on that number regardless.
`20` is `X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY` - the expected
result when the lab CA is not in the trust store and no `--cacert` was
given. The certificate was fine; the script was wrong.

Fixed: a `HAVE_CA` flag now gates the assertion - the chain is asserted
when a CA bundle is available and reported as `skipped` when it is not,
so the run no longer fails over a check it deliberately did not make.
Verify results are also decoded (`20` = issuer not found, `10` = expired,
`18` = self-signed, `99` = could not connect) instead of printing a bare
number.

`lab-ca.crt` is generated, not shipped - it is not in any bundle. To
enable the chain assertion in a new checkout:

```bash
kubectl get secret -n cert-manager lab-ca-secret \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > lab-ca.crt
```

## v2.12.2 - check-headers.sh compares the app against the CSP

Field report: after applying the v2.12.1 header ConfigMap while the
cluster still ran `file-web:2.9`, **both** `check-headers.sh` and
`check-app.sh` reported everything OK - and every page rendered
unstyled. The 2.9 image emits an inline `<style>` block, which
`style-src 'self'` refuses.

Neither script could see it, and that was structural rather than a bug:
`check-headers.sh` reads response headers, `check-app.sh` inspects pods,
DNS, volumes and schema. **A CSP is enforced in the browser**, and
nothing in either script renders a page. The policy was correct, the app
was correct, and the pair was wrong.

New section in `check-headers.sh`, between the header checks and the
transport checks. It fetches `/login` and compares the markup against the
policy actually in force:

| Condition | Verdict |
|---|---|
| `style-src` lacks `unsafe-inline` **and** the page has `<style>` | FAIL - "pages render UNSTYLED" |
| same, and the page has a `style=""` attribute | FAIL |
| same, and no `<link rel=stylesheet>` at all | FAIL |
| `script-src 'none'` and the page has `<script>` | FAIL |
| the linked stylesheet does not return 200 | FAIL |
| it returns 200 but not `text/css` | FAIL - nosniff would reject it |
| `style-src` allows `unsafe-inline` | note, not failure - a looser policy legitimately permits inline styles |

Verified against five simulated combinations: current image + strict CSP
(clean), 2.9 image + strict CSP (reproduces the exact failure), an inline
`style=` attribute, a `<script>` tag under `script-src 'none'`, and an
old image under a loose CSP (correctly reported as fine, not a failure).

The last two rows matter as much as the first: a stylesheet that 404s or
is served with the wrong MIME type produces the same unstyled page by a
different route, and `X-Content-Type-Options: nosniff` means the browser
will not guess.

Scripts only - no image rebuild, no manifest change.

---

# v2.13 - HTML-layer hardening

Images rebuild as **file-api:2.13 / file-web:2.13**. No schema change.
The audit that preceded this found five real gaps; ceremony that added
nothing was left out.

## What was already correct

Jinja autoescaping **is** on for `render_template_string` (verified, not
assumed - `select_jinja_autoescape(None)` returns True), downloads are
already `octet-stream` + `attachment`, and `script-src 'none'` already
makes HTML smuggling inert. Those now have tests so they cannot regress.

## 1. CSRF tokens - the real gap

`SameSite=Lax` was the *only* CSRF defence. It is client-enforced, and it
does not consider a compromised sibling host to be cross-site. Every
state-changing request now carries a synchroniser token:

- `csrf_token()` mints 32 random bytes into the signed session.
- Every `<form method=post>` embeds it as a hidden `_csrf` field
  (six forms; a test asserts none is ever missed).
- A `before_request` hook rejects any non-GET/HEAD/OPTIONS request whose
  token does not match, using `secrets.compare_digest`.
- Failure renders **400 "Request blocked"**, not a redirect: a forged
  request must fail visibly rather than bounce somewhere that looks like
  it worked.
- On login the session is cleared and the token regenerated, so a token
  handed to an anonymous visitor cannot survive a privilege change
  (session fixation).

## 2. `__Host-` session cookie

When `COOKIE_SECURE=true` the cookie is named `__Host-session`. The
prefix is **browser-enforced**: the cookie is rejected unless it is
Secure, `Path=/` and carries no `Domain`. A sibling host therefore cannot
inject or overwrite our session cookie.

## 3. `Cache-Control: no-store` on HTML

An authenticated page lists someone's private filenames. HTML responses
now carry `no-store, max-age=0, must-revalidate`, so the listing cannot
be recovered from the browser cache after the user walks away. The
stylesheet keeps its long cache (a test asserts both).

## 4. Deceptive characters in filenames

`sanitize_filename` previously stripped only NUL. It now removes C0/C1
controls, the bidirectional overrides (U+202A-202E, U+2066-2069) and
zero-width/BOM. The attack this stops is RIGHT-TO-LEFT OVERRIDE:

```
invoice\u202Egnp.exe      ->  displays to a human as  invoiceexe.png
```

A user sees an image and downloads an executable. Stripped rather than
rejected, so `résumé final.pdf` and `报告.txt` still work. Order matters:
the strip happens **before** `basename`, so an override cannot hide a
path separator from the traversal check.

## 5. Username charset

Was: any 3-64 characters. Now ASCII letters, digits, `.`, `_`, `-`,
starting and ending alphanumeric. This kills homoglyph impersonation -
`Bob` with a Cyrillic `о` is a different account that renders
identically - and means the "Signed in as X" line cannot be spoofed even
if escaping ever regressed. Existing accounts are unaffected;
validation runs at registration.

## 6. `Cross-Origin-Resource-Policy: same-origin`

Stops another origin loading our pages or downloads as a subresource, so
a hostile site cannot pull a file from here and re-serve it as its own.

## Tests

123 total, up from 103. New: six CSRF (missing token refused, wrong
token refused, correct token accepted, delete refused without one, every
POST form carries a hidden field, tokens differ per session), four
response-hardening (HTML is no-store, CSS still cacheable, a hostile
**filename** renders escaped rather than executed, a hostile flash
message is escaped), and ten deceptive-input tests.

Each guard was verified by breaking it: removing the CSRF comparison
failed 3 tests, removing `no-store` failed 1, and restoring the old
filename handling failed 4.

## Rolling out

```bash
./scripts/build-test.sh
docker push 192.168.1.210:5000/file-api:2.13
docker push 192.168.1.210:5000/file-web:2.13
kubectl apply -f k8s/api.yaml -f k8s/web.yaml
kubectl -n fileapp rollout status deploy/web --timeout=180s

kubectl apply -f k8s/ingress-nginx-headers.yaml     # adds CORP
kubectl -n ingress-nginx rollout restart deploy/ingress-nginx-controller
./scripts/check-headers.sh && ./scripts/check-app.sh
```

**Existing sessions are logged out** by this deploy: old cookies have no
`csrf` key, so their next POST is refused, and under `COOKIE_SECURE` the
cookie is renamed. Log in again - one-time.

## Limits worth stating

- A username registered before v2.13 may still contain anything.
  Autoescaping renders it safely; the charset rule only governs new
  accounts.
- CSRF protection assumes the session cookie is intact. It defends
  against forged cross-origin requests, not against an attacker who has
  already stolen the cookie - that is what HttpOnly, Secure, `__Host-`
  and the 60-minute JWT are for.
- Uploads are still unrestricted by type, deliberately: this is a file
  host. Safety comes from never rendering them (`octet-stream` +
  `attachment` + `nosniff`), not from guessing which types are dangerous.
