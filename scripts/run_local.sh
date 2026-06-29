#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

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

export USE_EMBEDDED_DB="${USE_EMBEDDED_DB:-true}"
export PGSERVER_DATA_DIR="${PGSERVER_DATA_DIR:-$ROOT/pgserver_data}"
export KHOJ_ADMIN_EMAIL="${KHOJ_ADMIN_EMAIL:-local@example.com}"
export KHOJ_ADMIN_PASSWORD="${KHOJ_ADMIN_PASSWORD:-local-dev-password}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-${DEEPSEEK_API_KEY:-}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}"
export KHOJ_DEFAULT_CHAT_MODEL="${KHOJ_DEFAULT_CHAT_MODEL:-deepseek-v4-flash}"

exec .venv/bin/khoj --host "${KHOJ_HOST:-127.0.0.1}" --port "${KHOJ_PORT:-42110}" --anonymous-mode --non-interactive "$@"
