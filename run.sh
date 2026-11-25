#!/usr/bin/env bash
# LLM gateway runner (Git Bash / Linux / macOS). Same commands as run.ps1.
#
#   ./run.sh              build + run end to end: validate config, fill .env, pull images,
#                         start, wait until healthy, provision teams / keys / Langfuse projects
#   ./run.sh test         end-to-end self-test of the running stack (temporary keys only)
#   ./run.sh provision    re-apply config/teams.yaml + hook setup (no restart)
#   ./run.sh render       only validate config/ and regenerate .build/
#   ./run.sh status | logs [svc] | compose <args> | pull | down
set -euo pipefail
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
ROOT=$(pwd -W 2>/dev/null || pwd)        # Windows path for Docker Desktop under Git Bash
CMD=${1:-up}; [[ $# -gt 0 ]] && shift
CENV=.build/compose.env

step() { echo; echo "==> $1"; }
die()  { echo "ERROR: $1" >&2; exit 1; }
envval() { { grep -E "^$1=" "$CENV" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed "s/^'//; s/'$//"; }
yamlval() { # yamlval images.litellm  (two-level keys of config/stack.yaml only)
  awk -v sec="${1%%.*}" -v key="${1#*.}" '
    /^[^ #][^:]*:/ { cur = $1; sub(":", "", cur) }
    cur == sec && $1 == key":" { v = $2; gsub(/["\047]/, "", v); print v; exit }' config/stack.yaml
}
dc() { [[ -f "$CENV" ]] || die "no $CENV yet - run ./run.sh first"; docker compose --env-file "$CENV" -f compose.yaml "$@"; }

docker info >/dev/null 2>&1 || die "Docker is not running."
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required."
mkdir -p .state

case "$CMD" in
  status)    dc ps; exit ;;
  logs)      dc logs -f --tail 100 "$@"; exit ;;
  compose)   dc "$@"; exit ;;
  down)      dc down; echo "Stopped. Data kept in the volumes."; exit ;;
  provision) step provision; dc run --rm -T provision; exit ;;
  test)      step self-test; dc run --rm -T selftest; exit ;;
  up|pull|render) ;;
  *) die "unknown command '$CMD' (up | test | provision | render | status | logs | compose | pull | down)" ;;
esac

LITE=$(yamlval images.litellm); PULL=$(yamlval images.pull); PULL=${PULL:-missing}
[[ -n "$LITE" ]] || die "config/stack.yaml: images.litellm is missing"
ensure_image() {
  local img=$1
  if [[ "$PULL" == always ]] || { [[ "$PULL" == missing ]] && ! docker image inspect "$img" >/dev/null 2>&1; }; then
    echo "  pull  $img"; docker pull -q "$img" >/dev/null || die "could not pull $img"
  elif ! docker image inspect "$img" >/dev/null 2>&1; then
    die "image $img is not on this machine and images.pull is 'never'"
  fi
}
render() { docker run --rm --pull never --user 0:0 --entrypoint python -v "$ROOT:/w" -w /w "$LITE" -I tools/render.py "$@"; }

step "1/6 config (config/*.yaml + .env -> .build/)"
ensure_image "$LITE"
render || die "fix the above and run again"
[[ "$CMD" == render ]] && exit 0

step "2/6 images (images.pull: $PULL)"
mapfile -t IMAGES < <(render --images | tr -d '\r')
for img in "${IMAGES[@]}"; do ensure_image "$img"; done
{ echo "# image digests in use - written by run.sh $(date -u +%FT%TZ)"
  for img in "${IMAGES[@]}"; do
    echo "$img  $(docker image inspect --format '{{index .RepoDigests 0}}' "$img" 2>/dev/null || echo '(local build)')"
  done; } > images.lock
echo "  ${#IMAGES[@]} images ready (digests in images.lock)"
[[ "$CMD" == pull ]] && exit 0

if [[ -s certs/corp-root-ca.pem ]]; then
  docker run --rm --pull never --user 0:0 --entrypoint python -v "$ROOT:/w" -w /w "$LITE" -I tools/build_ca_bundle.py \
    || die "could not build certs/ca-bundle.pem"
fi

step "3/6 start containers"
dc up -d --remove-orphans
dc restart litellm >/dev/null     # LiteLLM reads config + hooks only at start

step "4/6 wait until healthy"
HOST=$(envval BIND_ADDRESS); [[ "$HOST" == 0.0.0.0 ]] && HOST=127.0.0.1
check() {
  local name=$1 url=$2 i
  for i in $(seq 1 60); do
    if curl -fsS -o /dev/null --noproxy '*' --max-time 5 "$url" 2>/dev/null; then echo "  ok    $name"; return 0; fi
    sleep 5
  done
  echo "  FAIL  $name did not answer at $url (./run.sh logs $3)"; return 1
}
ok=true
for ((p = $(envval PORT_LITELLM); p <= $(envval PORT_LITELLM_LAST); p++)); do
  check "LiteLLM :$p" "http://$HOST:$p/health/liveliness" litellm || ok=false
done
check Langfuse   "http://$HOST:$(envval PORT_LANGFUSE)/api/public/health" langfuse-web || ok=false
check Grafana    "http://$HOST:$(envval PORT_GRAFANA)/api/health" grafana || ok=false
check Prometheus "http://$HOST:$(envval PORT_PROMETHEUS)/-/ready" prometheus || ok=false
code=$(dc ps -a --format '{{.ExitCode}}' db-setup 2>/dev/null | head -1)
if [[ "$code" == 0 ]]; then echo "  ok    db-setup"; else echo "  FAIL  db-setup (exit ${code:-?})"; ok=false; fi

step "5/6 provision (teams, keys, Langfuse projects, metrics key)"
if $ok; then dc run --rm -T provision || ok=false; else echo "  skipped (stack not healthy)"; fi

step "6/6 ready"
dc ps --format '  {{.Service}}: {{.State}} {{.Health}}' | sort -u
cat <<EOF

  LiteLLM API  $(envval LITELLM_PUBLIC_URL)          Bearer KEY_<TEAM>_<KEY> (apps) | LITELLM_MASTER_KEY (admin)
  LiteLLM UI   $(envval LITELLM_PUBLIC_URL)/ui       LITELLM_UI_USERNAME / LITELLM_UI_PASSWORD
  Langfuse     $(envval LANGFUSE_PUBLIC_URL)          LANGFUSE_ADMIN_EMAIL / LANGFUSE_ADMIN_PASSWORD
  Grafana      $(envval GRAFANA_PUBLIC_URL)          GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD
  Prometheus   $(envval PROMETHEUS_PUBLIC_URL)
  Credentials: .env (this folder).  Self-test: ./run.sh test
EOF
$ok || { echo; die "some checks failed - see above"; }
