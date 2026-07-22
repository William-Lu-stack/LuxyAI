#!/usr/bin/env bash
set -Eeuo pipefail

RAW_INSTALLER="https://raw.githubusercontent.com/your-org/Flawless/main/scripts/install.sh"

fail() {
  printf '[flawless-macos] ERROR: %s\n' "$*" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || fail "this installer only supports macOS"
command -v git >/dev/null 2>&1 \
  || fail "Git is required; run xcode-select --install first"

no_start=0
for argument in "$@"; do
  [ "$argument" = "--no-start" ] && no_start=1
done

if [ "$no_start" -eq 0 ]; then
  command -v docker >/dev/null 2>&1 \
    || fail "Docker Desktop is required: https://www.docker.com/products/docker-desktop/"
  docker info >/dev/null 2>&1 \
    || fail "Docker Desktop is installed but not running; start it and wait until the engine is ready"
  compose_version="$(docker compose version --short 2>/dev/null || true)"
  [[ "$compose_version" =~ ^v?2\. ]] \
    || fail "Docker Compose v2 is required; update Docker Desktop"
fi

export FLAWLESS_BRANCH=main
unset FLAWLESS_REPOSITORY_URL
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -e "${BASH_SOURCE[0]}" ]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -x "$script_dir/install.sh" ]; then
    exec "$script_dir/install.sh" "$@" --mode docker
  fi
fi

command -v curl >/dev/null 2>&1 || fail "curl is required to download the main-branch installer"
installer="$(mktemp -t flawless-installer.XXXXXX)"
trap 'rm -f "$installer"' EXIT
curl -fsSL --retry 3 "$RAW_INSTALLER" -o "$installer"
bash "$installer" "$@" --mode docker
