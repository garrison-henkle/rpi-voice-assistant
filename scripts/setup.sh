#!/usr/bin/env bash
# Day-one setup for the rpi-nest-mini stack.
# Pulls qwen3:1.7b, builds the tuned 'qwen3-nest-mini' tag from the Modelfile,
# warms the Piper voice, and brings all four services up.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
DIE() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null          || DIE "docker is not installed"
docker compose version >/dev/null 2>&1 || DIE "docker compose plugin is missing"
docker buildx version >/dev/null 2>&1 || DIE "BuildKit is required (piper builds from a Git URL). Try: 'docker buildx create --use'."

# Bootstrap local .env from the public .env.example if missing.
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    LOG "Created .env from .env.example — review LVA_USER_ID/GID before continuing"
  else
    DIE ".env and .env.example both missing; nothing to bootstrap from"
  fi
fi

export DOCKER_BUILDKIT=1

LOG "1/5  Starting ollama + piper first (so they warm before rpi-assistant)"
docker compose up -d ollama piper

LOG "2/5  Pulling qwen3:1.7b (≈1.4 GB)"
docker exec -it ollama ollama pull qwen3:1.7b

LOG "3/5  Building 'qwen3-nest-mini' tag from models/qwen3-nest-mini.Modelfile"
docker exec -it ollama ollama create qwen3-nest-mini -f /models/qwen3-nest-mini.Modelfile

LOG "4/5  Building + starting rpi-assistant"
docker compose build rpi-assistant
docker compose up -d rpi-assistant

LOG "5/5  Starting linux-voice-assistant"
docker compose up -d linux-voice-assistant

echo
LOG "Status:"
docker compose ps
echo
LOG "Following logs (Ctrl-C to detach):"
docker compose logs -f --tail=50
