#!/usr/bin/env bash
# =====================================================================
#  Server-side deployment script (run on the Singapore GPU host).
#  Pulls latest image and restarts the stack with zero-downtime intent.
# =====================================================================
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Create it from .env.example first." >&2
  exit 1
fi

log "Checking GPU visibility..."
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
else
  log "WARN: nvidia-smi not found - GPU inference will fall back to CPU."
fi

log "Pulling latest images..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull

log "Rolling restart..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --remove-orphans

log "Pruning dangling images..."
docker image prune -f

log "Health check (waiting for /metrics)..."
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:9090/metrics" >/dev/null 2>&1; then
    log "Bot is healthy."
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
    exit 0
  fi
  sleep 2
done

log "ERROR: bot did not become healthy in 60s." >&2
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=100 bot
exit 1
