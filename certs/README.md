# certs/ - corporate TLS inspection

If the network re-signs HTTPS (Netskope here), LiteLLM's calls to the LLM providers fail with
"certificate verify failed" unless it trusts the re-signing CA. Public certificates only - never a
private key (`build_ca_bundle.py` refuses one).

| File | What | Who makes it |
|---|---|---|
| `corp-root-ca.pem` | Corporate (Netskope) root CA | you - export it from the Windows trust store (see `tools/fetch-intermediate.sh`) |
| `corp-intermediate-ca.pem` | Netskope tenant intermediate (`ca.ness.goskope.com`) | `tools/fetch-intermediate.sh`, only if it verifies against the root |
| `ca-bundle.pem` | public CAs of the running LiteLLM image + root + intermediate | the runner, on **every** run, when `corp-root-ca.pem` exists |

LiteLLM uses the bundle via `SSL_CERT_FILE`. With no `corp-root-ca.pem` here, LiteLLM uses the
image's own CA list (right for networks without TLS inspection). Langfuse, Grafana and Prometheus
make no outbound calls in this stack, so they need nothing.

Why the intermediate: LiteLLM's Python 3.13 verifies in X509-strict mode and rejects the Netskope
root (`CA cert does not include key usage extension`). The intermediate has keyUsage and a chain may
end at any trusted certificate, so verification passes with strict mode and hostname checks still on.

If the network's CA changes: replace `corp-root-ca.pem`, delete `corp-intermediate-ca.pem`, run
`./tools/fetch-intermediate.sh` (Git Bash), then the runner. The `*.pem` files are git-ignored
(network-specific): copy this folder along when moving the stack to another machine.
