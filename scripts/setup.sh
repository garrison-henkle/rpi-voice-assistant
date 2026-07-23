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

# Quick disk check. qwen3:1.7b is ~1.4 GB and moonshine-medium adds
# ~450 MB on top of the un-modified pull layers; the Ollama volume +
# container layers live under /var/lib/docker on the Pi's root FS, so an
# almost-full SD card breaks the pull mid-flight.
free_mb=$(df -Pm /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')
if [[ -n "$free_mb" && "$free_mb" -lt 5120 ]]; then
  WARN "/var/lib/docker has only ${free_mb} MB free; need ~5 GB for qwen3:1.7b + moonshine-medium bake"
  WARN "Free up space or move /var/lib/docker to a bigger volume first"
fi

# Wait for a container's `healthcheck` to transition to `healthy`.
# Polls `docker inspect --format '{{.State.Health.Status}}'` once a second
# and bails after `[max_tries]` tries (~4 minutes is typical for ollama's
# `start_period: 90s + 15 retries × 10s` healthcheck from compose.yml).
#
# We intentionally poll the container's own healthcheck instead of curling
# `127.0.0.1:11434` from the host because every service in this stack now
# lives on the `stack` bridge, so ollama :11434 is not exposed on the host
# loopback and a host-side probe would spin for the full timeout window
# before failing (fixed in the streaming refactor — see docker-compose.yml).
wait_for_container_healthy() {
  local name="$1" max_tries="$2" tries elapsed=0
  while (( tries++ < max_tries )); do
    elapsed=$((elapsed + 1))
    local status
    status=$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)
    case "$status" in
      healthy)
        LOG "${name} healthy after ~${elapsed}s"
        return 0
        ;;
      starting|unhealthy|missing|"")
        if (( tries % 10 == 0 )); then
          printf '  [%s] %s container status: %s\n' "$(date +%H:%M:%S)" "$name" "$status"
        fi
        sleep 1
        ;;
    esac
  done
  WARN "${name} did not become healthy within ${max_tries}s"
  docker ps --filter name=^${name}$ --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' >&2 || true
  WARN "Last 20 lines of ${name} log:"
  docker logs --tail=20 "$name" >&2 || true
  return 1
}

export DOCKER_BUILDKIT=1

LOG "1/7  Starting ollama + piper first (so they warm before rpi-assistant)"
docker compose up -d ollama piper

LOG "2/7  Waiting for ollama healthy (compose healthcheck; max 4 minutes)"
wait_for_container_healthy ollama 240

LOG "2b/7 Waiting for piper healthy (compose healthcheck; max 4 minutes)"
wait_for_container_healthy piper 240

LOG "3/7  Pulling qwen3:1.7b (≈1.4 GB; progress prints inline)"
# `-i` only — let ollama print its progress to our stdout. Avoid `-t` so no
# TTY allocation is attempted (which can cause hard-to-debug exits on Pi over
# SSH if the upstream session isn't a real PTY).
docker exec -i ollama ollama pull qwen3:1.7b

LOG "4/7  Building 'qwen3-nest-mini' tag from models/qwen3-nest-mini.Modelfile"
# `ollama create` is idempotent; recreating over a stale tag is fine.
docker exec -i ollama ollama create qwen3-nest-mini -f /models/qwen3-nest-mini.Modelfile

LOG "5/7  Building rpi-assistant and assistant-satellite images"
docker compose build rpi-assistant assistant-satellite

LOG "6/7  Bringing up rpi-assistant + assistant-satellite"
docker compose up -d rpi-assistant assistant-satellite

LOG "7/7  Done — status + tail"
echo
docker compose ps
echo
LOG "Tail logs (Ctrl-C to detach):"
docker compose logs -f --tail=50
