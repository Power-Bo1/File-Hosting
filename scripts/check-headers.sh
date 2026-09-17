#!/usr/bin/env bash
# Assert the security response headers are actually on the wire.
# Answers "is it really set?" independently of what a browser UI claims.
#
#   ./scripts/check-headers.sh
#   HOST=files.local CA=lab-ca.crt ./scripts/check-headers.sh
set -uo pipefail

HOST="${HOST:-files.local}"
CA="${CA:-lab-ca.crt}"
FAILED=0

ok()   { printf '    %-32s OK\n'   "$1"; }
bad()  { printf '    %-32s FAIL  %s\n' "$1" "${2:-}"; FAILED=1; }
note() { printf '    %-32s %s\n' "$1" "${2:-}"; }

# HAVE_CA drives whether the certificate chain is ASSERTED or SKIPPED.
# Without it we fall back to -k, and curl still reports the underlying
# verification result in %{ssl_verify_result} - so treating that number
# as a failure would fail a check we deliberately chose not to run.
if [ -f "$CA" ]; then
  HAVE_CA=1
  CURL=(--cacert "$CA")
  echo "==> using CA bundle: $CA (the chain is asserted below)"
else
  HAVE_CA=0
  CURL=(-k)
  echo "==> no $CA found - headers still checked, chain verification SKIPPED"
  echo "    export the CA to enable it:"
  echo "      kubectl get secret -n cert-manager lab-ca-secret \\"
  echo "        -o jsonpath='{.data.tls\\.crt}' | base64 -d > ${CA}"
fi

echo
echo "==> GET https://${HOST}/login"
HEADERS=$(curl -sS -D - -o /dev/null "${CURL[@]}" "https://${HOST}/login" 2>&1) || {
  echo "FAIL: could not fetch https://${HOST}/login"; echo "$HEADERS"; exit 1; }
echo "$HEADERS" | head -1

expect() {   # $1 header name   $2 required substring   $3 optional label
  local label="${3:-$1}"
  if printf '%s\n' "$HEADERS" | grep -iq "^$1:.*$2"; then ok "$label"
  else bad "$label" "missing or not matching '$2'"; fi
}

echo
echo "==> Response headers"
expect "strict-transport-security"  "max-age"
expect "x-content-type-options"     "nosniff"
expect "x-frame-options"            "DENY"
expect "referrer-policy"            "no-referrer"
expect "permissions-policy"         "camera"
expect "content-security-policy"    "default-src"
expect "content-security-policy"    "script-src 'none'"        "  CSP: script-src 'none'"
expect "content-security-policy"    "frame-ancestors 'none'"   "  CSP: frame-ancestors"
expect "content-security-policy"    "upgrade-insecure-requests" "  CSP: upgrade-insecure-requests"
# Absence check: a policy can look strict and still carry the one
# directive that undoes it.
if printf '%s\n' "$HEADERS" | grep -iq "^content-security-policy:.*unsafe-inline"; then
  bad "  CSP: no 'unsafe-inline'" "still present in style-src"
else
  ok "  CSP: no 'unsafe-inline'"
fi
expect "cross-origin-opener-policy" "same-origin"
expect "cross-origin-resource-policy" "same-origin"
expect "cache-control"              "no-store"                  "  HTML is no-store"

echo
echo "==> App vs deployed CSP (the mismatch neither side can see alone)"
# A CSP is enforced in the BROWSER. Reading response headers proves the
# policy shipped; it says nothing about whether the running app obeys it.
# Deploying style-src 'self' while an older image still emits an inline
# <style> block leaves every page unstyled - and both check-headers.sh
# and check-app.sh pass, because neither renders a page. This section
# compares the two directly.
BODY=$(curl -sS "${CURL[@]}" "https://${HOST}/login" 2>/dev/null || true)
CSP_LINE=$(printf '%s\n' "$HEADERS" | grep -i '^content-security-policy:' || true)

if [ -z "$BODY" ]; then
  bad "fetch /login body" "could not retrieve page"
elif [ -z "$CSP_LINE" ]; then
  bad "content-security-policy" "no CSP header to compare against"
else
  if printf '%s' "$CSP_LINE" | grep -qi "unsafe-inline"; then
    note "style-src allows 'unsafe-inline'" "inline styles permitted - policy is looser than v2.12"
  else
    if printf '%s' "$BODY" | grep -qi '<style'; then
      bad "inline <style> under style-src 'self'" "browser refuses it - pages render UNSTYLED"
    else
      ok "no inline <style> block"
    fi
    if printf '%s' "$BODY" | grep -qiE '[[:space:]]style="'; then
      bad "style=\"\" attribute under style-src 'self'" "browser refuses it"
    else
      ok "no inline style= attribute"
    fi
    if printf '%s' "$BODY" | grep -qiE '<link[^>]+stylesheet'; then
      ok "page links an external stylesheet"
    else
      bad "no stylesheet linked" "page has no styling source at all"
    fi
  fi

  if printf '%s' "$CSP_LINE" | grep -qi "script-src 'none'"; then
    if printf '%s' "$BODY" | grep -qi '<script'; then
      bad "<script> under script-src 'none'" "browser refuses it"
    else
      ok "no <script> tag"
    fi
  fi

  # The linked stylesheet must actually be fetchable and be CSS - a 404
  # here is the same unstyled outcome by a different route.
  HREF=$(printf '%s' "$BODY" \
    | grep -oiE '<link[^>]+href="[^"]*\.css[^"]*"' \
    | grep -oE 'href="[^"]*"' | head -1 | sed 's/^href="//; s/"$//')
  if [ -n "$HREF" ]; then
    case "$HREF" in /*) CSS_URL="https://${HOST}${HREF}" ;; *) CSS_URL="$HREF" ;; esac
    CSS_HDRS=$(curl -sS -D - -o /dev/null "${CURL[@]}" "$CSS_URL" 2>/dev/null || true)
    CSS_CODE=$(printf '%s\n' "$CSS_HDRS" | awk 'NR==1{print $2}')
    if [ "$CSS_CODE" = "200" ]; then ok "stylesheet fetches (200)"
    else bad "stylesheet ${HREF}" "returned ${CSS_CODE:-no response}"; fi
    if printf '%s\n' "$CSS_HDRS" | grep -qi '^content-type:.*text/css'; then
      ok "stylesheet served as text/css"
    else
      bad "stylesheet content-type" "not text/css - nosniff will reject it"
    fi
  fi
fi

echo
echo "==> Transport"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST}/")
case "$code" in
  301|308) ok "http -> https redirect (${code})" ;;
  *)       bad "http -> https redirect" "got ${code}" ;;
esac

if [ "$HAVE_CA" = "1" ]; then
  tls=$(curl -sS -o /dev/null -w '%{ssl_verify_result}' "${CURL[@]}" \
        "https://${HOST}/login" 2>/dev/null || echo 99)
  case "$tls" in
    0)  ok "certificate chain verifies" ;;
    20) bad "certificate chain" "20 = issuer not found: ${CA} is not the CA that signed this cert" ;;
    10) bad "certificate chain" "10 = certificate expired" ;;
    18) bad "certificate chain" "18 = self-signed cert not in ${CA}" ;;
    99) bad "certificate chain" "could not connect to https://${HOST}/login" ;;
    *)  bad "certificate chain" "openssl verify result ${tls}" ;;
  esac
else
  note "certificate chain" "skipped (no ${CA}; re-run with it to assert)"
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "ALL HEADER CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit "$FAILED"
