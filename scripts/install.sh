#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPOSITORY_URL="https://github.com/your-org/Flawless.git"
REPOSITORY_URL="${FLAWLESS_REPOSITORY_URL:-$DEFAULT_REPOSITORY_URL}"
RELEASE_BRANCH="${FLAWLESS_BRANCH:-main}"
DEFAULT_INSTALL_DIR="$HOME/Flawless"
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -e "${BASH_SOURCE[0]}" ]; then
  script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
  if [ -n "$script_root" ] && [ -d "$script_root/.git" ]; then
    DEFAULT_INSTALL_DIR="$script_root"
  fi
fi
INSTALL_DIR="${FLAWLESS_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
NO_START=0
QUICKSTART_ARGS=()

log() {
  printf '[flawless-installer] %s\n' "$*"
}

fail() {
  printf '[flawless-installer] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Install or update Flawless from the canonical main branch, then start it.

Usage:
  ./scripts/install.sh [--dir PATH] [--no-start] [quickstart options]

Examples:
  ./scripts/install.sh
  ./scripts/install.sh --china
  ./scripts/install.sh --dir "$HOME/Flawless" --mode native
  ./scripts/install.sh --no-start
EOF
}

canonical_remote() {
  local value="${1%.git}"
  value="${value#ssh://git@github.com/}"
  value="${value#git@github.com:}"
  value="${value#https://github.com/}"
  value="${value#http://github.com/}"
  printf '%s' "$value"
}

validate_existing_remote() {
  local actual expected
  actual="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  [ -n "$actual" ] || fail "$INSTALL_DIR has no origin remote"
  if [ "$REPOSITORY_URL" = "$DEFAULT_REPOSITORY_URL" ]; then
    [ "$(canonical_remote "$actual")" = "your-org/Flawless" ] \
      || fail "$INSTALL_DIR points to an unexpected origin: $actual"
  else
    expected="${REPOSITORY_URL%/}"
    [ "${actual%/}" = "$expected" ] \
      || fail "$INSTALL_DIR points to $actual instead of $REPOSITORY_URL"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)
      [ "$#" -ge 2 ] || fail "--dir requires a path"
      INSTALL_DIR="$2"
      shift
      ;;
    --no-start) NO_START=1 ;;
    -h|--help) usage; exit 0 ;;
    *) QUICKSTART_ARGS+=("$1") ;;
  esac
  shift
done

command -v git >/dev/null 2>&1 || fail "git is required"

was_existing=0
before_revision=""
if [ -e "$INSTALL_DIR" ]; then
  [ -d "$INSTALL_DIR/.git" ] || fail "$INSTALL_DIR exists but is not a Git checkout"
  was_existing=1
  validate_existing_remote
  dirty="$(git -C "$INSTALL_DIR" status --porcelain)"
  if [ -n "$dirty" ]; then
    printf '%s\n' "$dirty" >&2
    fail "local changes detected; commit or stash them before updating"
  fi
  current_branch="$(git -C "$INSTALL_DIR" branch --show-current)"
  [ "$current_branch" = "$RELEASE_BRANCH" ] \
    || fail "$INSTALL_DIR must be on branch $RELEASE_BRANCH (current: ${current_branch:-detached})"
  before_revision="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  log "fetching the latest origin/$RELEASE_BRANCH"
  git -C "$INSTALL_DIR" fetch --prune origin "$RELEASE_BRANCH"
  git -C "$INSTALL_DIR" merge --ff-only "origin/$RELEASE_BRANCH"
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  log "cloning $REPOSITORY_URL into $INSTALL_DIR"
  git clone --depth 1 --branch "$RELEASE_BRANCH" --single-branch "$REPOSITORY_URL" "$INSTALL_DIR"
fi

revision="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
remote_revision="$(git -C "$INSTALL_DIR" rev-parse "origin/$RELEASE_BRANCH")"
[ "$revision" = "$remote_revision" ] \
  || fail "installed revision does not exactly match origin/$RELEASE_BRANCH"
log "verified latest revision: ${revision:0:12}"

if [ "$NO_START" -eq 1 ]; then
  log "update complete; start skipped"
  exit 0
fi

if [ "$was_existing" -eq 1 ] && [ "$before_revision" != "$revision" ]; then
  log "stopping the previous revision before restart"
  "$INSTALL_DIR/scripts/quickstart.sh" stop >/dev/null 2>&1 || true
fi

exec "$INSTALL_DIR/scripts/quickstart.sh" "${QUICKSTART_ARGS[@]}"
