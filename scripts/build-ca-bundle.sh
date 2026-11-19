#!/usr/bin/env bash
# Corporate TLS inspection (e.g. Netskope/Zscaler) re-signs HTTPS with a company CA that
# containers don't trust. This builds certs/ca-bundle.pem =
#   public CAs (from the LiteLLM image's certifi) + certs/corp-root-ca.pem + certs/corp-intermediate-ca.pem
#
# 1. Root CA -> certs/corp-root-ca.pem. Windows: export it from the trust store, e.g. (PowerShell)
#      $c = Get-ChildItem Cert:\LocalMachine\Root | ? Subject -like '*Netskope*' | select -First 1
#    then save $c.RawData as base64 between BEGIN/END CERTIFICATE lines.
#
# 2. Intermediate CA -> certs/corp-intermediate-ca.pem (fetched automatically below if missing).
#    Why: Python 3.13 (LiteLLM) verifies in X509-strict mode and rejects the Netskope ROOT because it
#    has no keyUsage extension ("CA cert does not include key usage extension"). The tenant
#    intermediate (ca.ness.goskope.com) does have it. Python 3.13 also allows a chain to end at any
#    trusted cert, so trusting the intermediate makes verification succeed with every check still on.
#    The intermediate is accepted only if `openssl verify` proves it is signed by the root.
#
# Who uses what:  LiteLLM -> SSL_CERT_FILE=ca-bundle.pem   Go services -> SSL_CERT_DIR includes /certs
#                 Langfuse (Node) -> NODE_EXTRA_CA_CERTS=corp-root-ca.pem
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
img=$(grep -E '^LITELLM_IMAGE=' .env 2>/dev/null | cut -d= -f2-); img=${img:-ghcr.io/berriai/litellm:main-stable}
probe_host=${CA_PROBE_HOST:-api.openai.com}

fetch_intermediate() {
  # Grab the chain the proxy presents for $probe_host and keep the CA cert the root signed.
  command -v openssl >/dev/null || { echo "  (openssl not found - add certs/corp-intermediate-ca.pem by hand)"; return 0; }
  # relative temp dir: a /tmp path would not resolve for a native Windows openssl under Git Bash
  local tmp="certs/.chain.$$"; mkdir -p "$tmp"
  openssl s_client -connect "$probe_host:443" -servername "$probe_host" -showcerts </dev/null 2>/dev/null \
    | awk -v d="$tmp" '/BEGIN CERT/{n++} n{print > (d "/c" n ".pem")}' || true
  for f in "$tmp"/c*.pem; do
    [[ -e $f ]] || continue
    if openssl x509 -in "$f" -noout -ext basicConstraints 2>/dev/null | grep -q 'CA:TRUE' \
       && ! cmp -s <(openssl x509 -in "$f" -outform der) <(openssl x509 -in certs/corp-root-ca.pem -outform der) \
       && openssl verify -CAfile certs/corp-root-ca.pem "$f" >/dev/null 2>&1; then
      openssl x509 -in "$f" -out certs/corp-intermediate-ca.pem
      echo "  fetched intermediate: $(openssl x509 -in "$f" -noout -subject)"
      break
    fi
  done
  rm -rf "$tmp"
}

docker run --rm --entrypoint python "$img" -c "import certifi,sys; sys.stdout.write(open(certifi.where()).read())" > certs/ca-bundle.pem
if [[ -s certs/corp-root-ca.pem ]]; then
  { echo; echo "# --- corporate root CA (certs/corp-root-ca.pem)"; cat certs/corp-root-ca.pem; } >> certs/ca-bundle.pem
  [[ -s certs/corp-intermediate-ca.pem ]] || fetch_intermediate
  if [[ -s certs/corp-intermediate-ca.pem ]]; then
    openssl verify -CAfile certs/corp-root-ca.pem certs/corp-intermediate-ca.pem >/dev/null \
      || { echo "certs/corp-intermediate-ca.pem is NOT signed by certs/corp-root-ca.pem - refusing"; exit 1; }
    { echo; echo "# --- corporate intermediate CA (certs/corp-intermediate-ca.pem)"; cat certs/corp-intermediate-ca.pem; } >> certs/ca-bundle.pem
    echo "certs/ca-bundle.pem = public CAs + corp root CA + corp intermediate CA"
  else
    echo "certs/ca-bundle.pem = public CAs + corp root CA (no intermediate - LiteLLM/Python 3.13 may still fail)"
  fi
else
  : > certs/corp-root-ca.pem
  echo "certs/ca-bundle.pem = public CAs only (no certs/corp-root-ca.pem found)"
fi
