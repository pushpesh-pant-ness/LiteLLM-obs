# Corporate TLS inspection certificates

If your network re-signs HTTPS (Netskope, Zscaler, ...), containers fail outbound calls with
"certificate signed by unknown authority". Put the company root CA here as `corp-root-ca.pem`
(PEM, public certificate only - never a private key), then run `scripts/build-ca-bundle.sh`.

- `corp-root-ca.pem`         - the corporate root (Langfuse via NODE_EXTRA_CA_CERTS; Go services via SSL_CERT_DIR)
- `corp-intermediate-ca.pem` - the Netskope tenant intermediate (`ca.ness.goskope.com`); fetched by the
  script if missing, and only accepted if it verifies against the root
- `ca-bundle.pem`            - public CAs + root + intermediate (LiteLLM via SSL_CERT_FILE)

Why the intermediate: LiteLLM runs Python 3.13, which verifies in X509-strict mode and rejects the
Netskope root (`CA cert does not include key usage extension`). The intermediate has keyUsage, and
Python 3.13 lets a chain end at any trusted certificate, so with it in the bundle verification
succeeds with strict mode and hostname checks still on. Nothing is relaxed.

All are git-ignored (they are network-specific). With no files here, every service uses its normal public CA list.
If the network's CA changes, delete `corp-intermediate-ca.pem`, rerun the script and `docker compose restart litellm`.
