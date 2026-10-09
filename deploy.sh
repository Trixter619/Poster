#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo 'Установи Docker Engine или Docker Desktop с Compose.' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
if [[ ! -f .env ]]; then cp .env.example .env; chmod 600 .env; fi
compose=(docker compose -f compose.yaml)
case "${1:-}" in
  --public) compose+=(-f compose.public.yaml) ;;
  "") ;;
  *) echo 'Использование: ./deploy.sh [--public]' >&2; exit 1 ;;
esac
"${compose[@]}" config --quiet
"${compose[@]}" build app
"${compose[@]}" run --rm --no-deps app python bootstrap_admin.py
"${compose[@]}" up -d --wait --wait-timeout 180
echo 'VK Poster запущен. Локальная панель: http://localhost:8787; публичный адрес — DOMAIN из .env.'
