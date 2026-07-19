#!/usr/bin/env bash
# Day-one setup for the rpi-nest-mini stack.
# Starts ollama + piper, pulls qwen3:1.7b, builds the tuned
# 'qwen3-nest-mini' tag from the Modelfile, then starts rpi-assistant
# and linux-voice-assistant.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
WARN() { printf '\033[1;33m??\033[0m %s\n' "$*" >&2; }
DIE()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null          || DIE "docker is not installed"
docker compose version >/dev/null 2>&1 || DIE "docker compose plugin is missing"
docker buildx version >/dev/null 2>&1 || DIE "BuildKit is required (piper builds from a Git URL). Try: 'docker buildx create --use'."

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    LOG "Created .env from .env.example — review LVA_USER_ID/GID before continuing"
  else
    DIE ".env and .env.example both missing; nothing to bootstrap from"
  fi
fi

# Quick disk check. qwen3:1.7b is ~1.4 GB and the Ollama volume lives under
# /var/lib/docker on the Pi's root FS, so an almost-full SD card stops the
# pull mid-flight with a cryptic exit code.
free_mb=$(df -Pm /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -n "$free_mb" && "$free_mb" -lt 4096 ]]; then
  WARN "/var/lib/docker has only ${free_mb} MB free; need ~3 GB for qwen3:1.7b"
  WARN "Free up space or move /var/lib/docker to a bigger volume first"
fi

export DOCKER_BUILDKIT=1

# Wait for an Ollama daemon to respond to the tags endpoint. Returns 0 on
# success, 1 after timeout. Uses plain `docker exec` (no -it) so it works in
# non-TTY contexts and fails loudly under `set -e`.
wait_for_ollama() {
  local tries=60
  while (( tries-- > 0 )); do
    if docker exec ollama curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  WARN "ollama did not answer on :11434 within 60 s"
  WARN "Try:  docker logs ollama  (then:  systemctl status docker)"
  return 1
}

LOG "1/5  Starting ollama + piper first (so they warm before rpi-assistant)"
docker compose up -d ollama piper
wait_for_ollama

LOG "2/5  Pulling qwen3:1.7b (≈1.4 GB; progress prints inline)"
# `-i` only — let ollama print its progress to our stdout. Avoid `-t` so no
# TTY allocation is attempted (which can cause hard-to-debug exits on Pi over
# SSH if the upstream session isn't a real PTY).
docker exec -i ollama ollama pull qwen3:1.7b

LOG "3/5  Building 'qwen3-nest-mini' tag from models/qwen3-nest-mini.Modelfile"
# `ollama create` is idempotent; recreating over a stale tag is fine.
docker exec -i ollama ollama create qwen3-nest-mini -f /models/qwen3-nest-mini.Modelfile

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
