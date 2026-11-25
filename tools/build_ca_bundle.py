"""certs/ca-bundle.pem = public CAs (certifi of THIS LiteLLM image) + corp root + corp intermediate.

Run by run.ps1 / run.sh inside the LiteLLM image on every run when certs/corp-root-ca.pem exists,
so the public CA list always matches the image that is running:
    docker run --rm --entrypoint python -v <repo>:/w -w /w <litellm image> -I tools/build_ca_bundle.py

Why the intermediate: LiteLLM's Python 3.13 verifies in X509-strict mode and rejects the Netskope
root ("CA cert does not include key usage extension"). The tenant intermediate has keyUsage, and
a chain may end at any trusted cert, so with it in the bundle verification passes with every
check still on. Missing intermediate: tools/fetch-intermediate.sh (Git Bash, needs openssl).
"""

import os
import sys

import certifi

CERTS = "certs"
ROOT = os.path.join(CERTS, "corp-root-ca.pem")
INTER = os.path.join(CERTS, "corp-intermediate-ca.pem")
OUT = os.path.join(CERTS, "ca-bundle.pem")


def read_pem(path):
    with open(path, encoding="ascii") as f:
        pem = f.read().strip()
    if "PRIVATE KEY" in pem:
        sys.exit(f"  FAIL  {path} contains a private key - only public certificates belong here")
    if "BEGIN CERTIFICATE" not in pem:
        sys.exit(f"  FAIL  {path} is not a PEM certificate")
    return pem


def main():
    parts = [open(certifi.where(), encoding="ascii").read().rstrip()]
    parts.append("# --- corporate root CA (certs/corp-root-ca.pem)\n" + read_pem(ROOT))
    if os.path.exists(INTER) and os.path.getsize(INTER) > 0:
        parts.append("# --- corporate intermediate CA (certs/corp-intermediate-ca.pem)\n" + read_pem(INTER))
        what = "public CAs + corp root + corp intermediate"
    else:
        what = "public CAs + corp root (no intermediate: LiteLLM may still fail - tools/fetch-intermediate.sh)"
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write("\n\n".join(parts) + "\n")
    os.replace(tmp, OUT)
    print(f"  certs/ca-bundle.pem = {what}")


if __name__ == "__main__":
    main()
