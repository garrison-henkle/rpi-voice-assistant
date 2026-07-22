#!/usr/bin/env bash
# Day-one setup for the rpi-nest-mini stack.
# Docker builds the Kotlin jar (`docker compose build rpi-assistant`),
# so this host does NOT need a JDK. We do need `docker` + `docker compose`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
WARN() { printf '\033[1;33m??\033[0m %s\n' "$*" >&2; }
DIE()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null               || DIE "docker is not installed"
docker compose version >/dev/null 2>&1     || DIE "docker compose plugin is missing"
docker buildx version >/dev/null 2>&1      || DIE "BuildKit is required (piper builds from a Git URL). Try: 'docker buildx create --use'."

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    LOG "Created .env from .env.example — review SATELLITE_USER_ID/GID before continuing"
  else
    DIE ".env and .env.example both missing; nothing to bootstrap from"
  fi
fi

# Quick disk check. qwen3:1.7b is ~1.4 GB and faster-whisper base.en adds
# ~140 MB; the Ollama volume + container layers live under /var/lib/docker
# on the Pi's root FS, so an almost-full SD card breaks the pull mid-flight.
free_mb=$(df -Pm /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -n "$free_mb" && "$free_mb" -lt 4096 ]]; then
  WARN "/var/lib/docker has only ${free_mb} MB free; need ~3 GB for qwen3:1.7b + faster-whisper base.en"
  WARN "Free up space or move /var/lib/docker to a bigger volume first"
fi

# Wait for ollama to answer the tags endpoint. First-boot is slow on a Pi
# (SSH keypair generation + CPU inference-engine init can take 60-90 s), so
# we allow up to 3 minutes. We probe from the *host* rather than from inside
# the ollama container because the official ollama image is minimal and does
# not ship curl; probing inside produced a false-negative timeout even when
# the daemon was healthy. With `network_mode: host`, the daemon shares the
# host's :11434, so a host-side curl is the canonical liveness check.
wait_for_ollama() {
  local tries=180 elapsed=0
  while (( tries-- > 0 )); do
    elapsed=$((elapsed + 1))
    if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      LOG "ollama up after ${elapsed}s"
      return 0
    fi
    sleep 1
  done
  WARN "host :11434 did not answer within 3 minutes"
  WARN "is the ollama container running?"
  docker ps --filter name=^ollama$ --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' >&2 || true
  WARN "Last 20 lines of ollama log:"
  docker logs --tail=20 ollama >&2 || true
  return 1
}

export DOCKER_BUILDKIT=1

LOG "1/6  Starting ollama + piper first (so they warm before rpi-assistant)"
docker compose up -d ollama piper
wait_for_ollama

LOG "2/6  Pulling qwen3:1.7b (≈1.4 GB; progress prints inline)"
# `-i` only — let ollama print its progress to our stdout. Avoid `-t` so no
# TTY allocation is attempted (which can cause hard-to-debug exits on Pi over
# SSH if the upstream session isn't a real PTY).
docker exec -i ollama ollama pull qwen3:1.7b

LOG "3/6  Building 'qwen3-nest-mini' tag from models/qwen3-nest-mini.Modelfile"
# `ollama create` is idempotent; recreating over a stale tag is fine.
docker exec -i ollama ollama create qwen3-nest-mini -f /models/qwen3-nest-mini.Modelfile

LOG "4/6  Building faster-whisper, rpi-assistant and assistant-satellite images"
docker compose build faster-whisper rpi-assistant assistant-satellite

LOG "5/6  bringing up faster-whisper + rpi-assistant and waiting for healthy"
docker compose up -d faster-whisper rpi-assistant

LOG "6/6  starting assistant-satellite (mic + wake + audio)"
docker compose up -d assistant-satellite

echo
LOG "Status:"
docker compose ps
echo
LOG "Tail logs (Ctrl-C to detach):"
docker compose logs -f --tail=50
