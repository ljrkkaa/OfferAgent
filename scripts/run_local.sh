#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

declare -A explicit_overrides=()
for variable in KHOJ_HOST KHOJ_PORT KHOJ_ADMIN_EMAIL KHOJ_ADMIN_PASSWORD KHOJ_API_KEY; do
  if [[ -v "$variable" ]]; then
    explicit_overrides["$variable"]="${!variable}"
  fi
done

if [[ -f "$ROOT/.env" ]]; then
  if grep -q "=" "$ROOT/.env"; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/.env"
    set +a
  else
    raw_key="$(tr -d '[:space:]' < "$ROOT/.env")"
    if [[ -n "$raw_key" ]]; then
      export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-$raw_key}"
    fi
  fi
fi

for variable in "${!explicit_overrides[@]}"; do
  export "$variable=${explicit_overrides[$variable]}"
done

export USE_EMBEDDED_DB="${USE_EMBEDDED_DB:-true}"
export PGSERVER_DATA_DIR="${PGSERVER_DATA_DIR:-$ROOT/pgserver_data}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-${DEEPSEEK_API_KEY:-}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}"
export KHOJ_DEFAULT_CHAT_MODEL="${KHOJ_DEFAULT_CHAT_MODEL:-deepseek-v4-flash}"

host="${KHOJ_HOST:-127.0.0.1}"
args=(--host "$host" --port "${KHOJ_PORT:-42110}" --non-interactive)
if [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]; then
  export KHOJ_ADMIN_EMAIL="${KHOJ_ADMIN_EMAIL:-local@example.com}"
  export KHOJ_ADMIN_PASSWORD="${KHOJ_ADMIN_PASSWORD:-local-dev-password}"
  args+=(--anonymous-mode)
else
  : "${KHOJ_ADMIN_EMAIL:?KHOJ_ADMIN_EMAIL must be set explicitly for a non-loopback host}"
  : "${KHOJ_ADMIN_PASSWORD:?KHOJ_ADMIN_PASSWORD must be set explicitly for a non-loopback host}"
  : "${KHOJ_API_KEY:?KHOJ_API_KEY must be set explicitly for a non-loopback host}"
  if [[ ! "$KHOJ_API_KEY" =~ ^kk-[A-Za-z0-9_-]+$ || ${#KHOJ_API_KEY} -gt 50 ]]; then
    echo "KHOJ_API_KEY must use the kk- prefix, URL-safe characters, and at most 50 characters" >&2
    exit 1
  fi
fi

exec .venv/bin/khoj "${args[@]}" "$@"
