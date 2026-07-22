#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

failed=0
scan() {
  local label="$1"
  local pattern="$2"
  if git grep -n -I -E "$pattern" -- . ':!scripts/check-public-sanitization.sh'; then
    echo "[public-sanitization] blocked: $label" >&2
    failed=1
  fi
}

# Documentation-only ranges 192.0.2.0/24, 198.51.100.0/24 and
# 203.0.113.0/24 are allowed. Real RFC1918 addresses are not.
scan "RFC1918 IPv4 address" '(^|[^0-9])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3})([^0-9]|$)'
scan "embedded bearer token" 'Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}'
scan "Rancher-style API token" 'token-[A-Za-z0-9]+:[A-Za-z0-9]{12,}'
scan "private key material" 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
scan "credential embedded in URL" 'https?://[^/@[:space:]]+:[^/@[:space:]]+@'
scan "cloud or provider access key" '(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9_-]{20,})'

# Block quoted credential literals in source/YAML/JSON while allowing runtime
# variables, Secret references and redaction fixtures assembled at runtime.
credential_literal_pattern="(^|[^A-Za-z0-9_])([\"']?(password|passwd|client_secret|api_key|access_token|bearer_token|rancher_token|private_key|secret_key)[\"']?[[:space:]]*[:=][[:space:]]*[bfru]*[\"'][^<\$\\{][^\"']{2,}[\"'])"
credential_literals="$(git grep -n -I -E "$credential_literal_pattern" -- . ':!scripts/check-public-sanitization.sh' || true)"
if [[ -n "$credential_literals" ]]; then
  printf '%s\n' "$credential_literals"
  echo "[public-sanitization] blocked: quoted plaintext credential assignment" >&2
  failed=1
fi

dotenv_key_pattern='([A-Z0-9_]*(_PASSWORD|_PASSWD|_CLIENT_SECRET|_API_KEY|_ACCESS_TOKEN|_BEARER_TOKEN|_RANCHER_TOKEN|_PRIVATE_KEY|_SECRET_KEY)|(PASSWORD|PASSWD|CLIENT_SECRET|API_KEY|ACCESS_TOKEN|BEARER_TOKEN|RANCHER_TOKEN|PRIVATE_KEY|SECRET_KEY))'
dotenv_credentials="$(git grep -n -I -E "^[[:space:]]*(export[[:space:]]+)?${dotenv_key_pattern}[[:space:]]*=[[:space:]]*[^[:space:]#]+" -- . ':!scripts/check-public-sanitization.sh' || true)"
dotenv_credentials="$(printf '%s\n' "$dotenv_credentials" | grep -Ev '=[[:space:]]*(\$\{|<|""|os\.getenv|os\.environ\.get)' || true)"
if [[ -n "$dotenv_credentials" ]]; then
  printf '%s\n' "$dotenv_credentials"
  echo "[public-sanitization] blocked: non-empty plaintext credential environment value" >&2
  failed=1
fi

credential_default_pattern="(getenv|environ\\.get)\\([\"']${dotenv_key_pattern}[\"'][[:space:]]*,[[:space:]]*[\"'][^\"']+[\"']"
credential_defaults="$(git grep -n -I -E "$credential_default_pattern" -- . ':!scripts/check-public-sanitization.sh' || true)"
if [[ -n "$credential_defaults" ]]; then
  printf '%s\n' "$credential_defaults"
  echo "[public-sanitization] blocked: plaintext credential fallback" >&2
  failed=1
fi

email_matches="$(git grep -n -I -E '[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}' -- . ':!scripts/check-public-sanitization.sh' || true)"
email_matches="$(printf '%s\n' "$email_matches" | grep -Eiv '(@([A-Za-z0-9-]+\.)*example\.(com|org|net|invalid)([^A-Za-z]|$)|git@github\.com)' || true)"
if [[ -n "$email_matches" ]]; then
  printf '%s\n' "$email_matches"
  echo "[public-sanitization] blocked: non-example email address" >&2
  failed=1
fi

if [[ "$failed" -ne 0 ]]; then
  exit 1
fi

echo "[public-sanitization] no private address, plaintext credential, private key, or personal email detected"
