#!/usr/bin/env bash
# Cluster-side deploy for the file-host-k8s stack (v2: users + HTTPS).
# Run from a machine with kubectl + helm pointed at your cluster.
#
# PRE-REQUISITES this script does NOT do (see README / UPGRADE):
#   1. Ingress controller (Traefik or ingress-nginx) + MetalLB installed.
#   2. containerd on every node trusts the insecure registry.
#   3. Images built and pushed:  file-api:2.13  file-web:2.13
#   4. Real secrets created from the k8s/*-secret.example.yaml templates.
#
REGISTRY_IP="${REGISTRY_IP:-192.168.1.210}"
INGRESS_CLASS="${INGRESS_CLASS:-nginx}"   # or "traefik"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.20.2}"

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in postgres-secret auth-secret grafana-secret; do
  if [ ! -f "${HERE}/k8s/${f}.yaml" ]; then
    echo "ERROR: k8s/${f}.yaml missing. Copy the template and fill it in:"
    echo "       cp ${HERE}/k8s/${f}.example.yaml ${HERE}/k8s/${f}.yaml"
    exit 1
  fi
  if grep -q CHANGE_ME "${HERE}/k8s/${f}.yaml"; then
    echo "ERROR: k8s/${f}.yaml still has CHANGE_ME placeholders"; exit 1
  fi
done

echo "==> 1/8 Storage provisioner (local-path)"
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml
kubectl patch storageclass local-path \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' || true

echo "==> 2/8 In-cluster registry"
kubectl apply -f "${HERE}/k8s/registry.yaml"
kubectl -n registry rollout status deploy/registry --timeout=180s

echo
echo "    >>> BUILD & PUSH YOUR IMAGES NOW (if you haven't):"
echo "        docker build -t ${REGISTRY_IP}:5000/file-api:2.13 ${HERE}/api && docker push ${REGISTRY_IP}:5000/file-api:2.13"
echo "        docker build -t ${REGISTRY_IP}:5000/file-web:2.13 ${HERE}/web && docker push ${REGISTRY_IP}:5000/file-web:2.13"
echo "    Press Enter once the images are pushed..."
read -r _

echo "==> 3/8 cert-manager ${CERT_MANAGER_VERSION}"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
kubectl -n cert-manager rollout status deploy/cert-manager --timeout=180s
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=180s
kubectl -n cert-manager rollout status deploy/cert-manager-cainjector --timeout=180s

echo "==> 4/8 Lab CA issuer (retries while webhook warms up)"
for i in $(seq 1 10); do
  if kubectl apply -f "${HERE}/k8s/cert-manager-ca-issuer.yaml"; then break; fi
  echo "    webhook not ready yet, retry ${i}/10"; sleep 6
done

echo "==> 5/8 Secrets + PostgreSQL"
kubectl apply -f "${HERE}/k8s/namespace.yaml"
kubectl apply -f "${HERE}/k8s/postgres-secret.yaml"
kubectl apply -f "${HERE}/k8s/auth-secret.yaml"
kubectl apply -f "${HERE}/k8s/postgres.yaml"
kubectl apply -f "${HERE}/k8s/postgres-backup.yaml"
kubectl apply -f "${HERE}/k8s/files-backup.yaml"
kubectl apply -f "${HERE}/k8s/mailpit.yaml"
kubectl -n fileapp rollout status deploy/postgres --timeout=180s

echo "==> 6/8 API v2 (Image 2)"
kubectl apply -f "${HERE}/k8s/api.yaml"
kubectl -n fileapp rollout status deploy/api --timeout=180s

echo "==> 7/8 Website v2 (Image 1) + TLS ingress"
sed "s/ingressClassName: .*/ingressClassName: ${INGRESS_CLASS}/" "${HERE}/k8s/web.yaml" | kubectl apply -f -
kubectl -n fileapp rollout status deploy/web --timeout=180s
echo "    Waiting for certificate..."
kubectl -n fileapp wait certificate/files-local-tls --for=condition=Ready --timeout=180s || \
  echo "    (certificate not Ready yet - check: kubectl describe certificate -n fileapp files-local-tls)"

echo "==> Security response headers on the ingress controller"
if kubectl get ns ingress-nginx >/dev/null 2>&1; then
  kubectl apply -f "${HERE}/k8s/ingress-nginx-headers.yaml"
  kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p '{"data":{
    "add-headers":"ingress-nginx/custom-response-headers",
    "hsts":"true","hsts-max-age":"31536000",
    "hsts-include-subdomains":"true","hsts-preload":"false"}}' || true
  kubectl -n ingress-nginx rollout restart deploy/ingress-nginx-controller || true
else
  echo "    (ingress-nginx namespace not found - skipping)"
fi

echo "==> 8/8 Logging stack (Loki + Grafana + Alloy)"
kubectl create namespace logging --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "${HERE}/k8s/grafana-secret.yaml"
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install loki    grafana/loki    -n logging -f "${HERE}/helm/loki-values.yaml"
helm upgrade --install grafana grafana/grafana -n logging -f "${HERE}/helm/grafana-values.yaml"
helm upgrade --install alloy   grafana/alloy   -n logging -f "${HERE}/helm/alloy-values.yaml"
kubectl apply -f "${HERE}/k8s/alloy-rbac.yaml"
kubectl -n logging rollout restart daemonset/alloy || true

echo
echo "==> Done. Next steps:"
echo "    - /etc/hosts on your workstation:  <INGRESS_IP> files.local"
echo "    - Trust the lab CA (see UPGRADE.md, Phase D step 4)"
echo "    - Browse: https://files.local"
