#!/usr/bin/env bash
# Only needed when certs/corp-intermediate-ca.pem is missing (new network / CA changed).
# Grabs the chain the TLS-inspection proxy presents for a public host and keeps the CA cert that
# certs/corp-root-ca.pem signed. Accepted only if `openssl verify` proves it. Git Bash / Linux.
#
#   ./tools/fetch-intermediate.sh           # then re-run ./run.sh or .\run.ps1
#
# Root CA first -> certs/corp-root-ca.pem. Windows (PowerShell), e.g.:
#   $c = Get-ChildItem Cert:\LocalMachine\Root | ? Subject -like '*Netskope*' | select -First 1
#   then save $c.RawData as base64 between -----BEGIN/END CERTIFICATE----- lines.
set -euo pipefail
cd "$(dirname "$0")/.."
probe_host=${CA_PROBE_HOST:-api.openai.com}

command -v openssl >/dev/null || { echo "openssl not found - add certs/corp-intermediate-ca.pem by hand"; exit 1; }
[[ -s certs/corp-root-ca.pem ]] || { echo "certs/corp-root-ca.pem is missing (see the comment at the top)"; exit 1; }

# relative temp dir: a /tmp path would not resolve for a native Windows openssl under Git Bash
tmp="certs/.chain.$$"; mkdir -p "$tmp"; trap 'rm -rf "$tmp"' EXIT
openssl s_client -connect "$probe_host:443" -servername "$probe_host" -showcerts </dev/null 2>/dev/null \
  | awk -v d="$tmp" '/BEGIN CERT/{n++} n{print > (d "/c" n ".pem")}' || true
for f in "$tmp"/c*.pem; do
  [[ -e $f ]] || continue
  if openssl x509 -in "$f" -noout -ext basicConstraints 2>/dev/null | grep -q 'CA:TRUE' \
     && ! cmp -s <(openssl x509 -in "$f" -outform der) <(openssl x509 -in certs/corp-root-ca.pem -outform der) \
     && openssl verify -CAfile certs/corp-root-ca.pem "$f" >/dev/null 2>&1; then
    openssl x509 -in "$f" -out certs/corp-intermediate-ca.pem
    echo "fetched: $(openssl x509 -in "$f" -noout -subject)"
    exit 0
  fi
done
echo "no intermediate signed by certs/corp-root-ca.pem found in the chain for $probe_host"
exit 1
