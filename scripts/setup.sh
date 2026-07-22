#!/usr/bin/env bash
# Day-one setup for the rpi-nest-mini stack.
# Builds the Kotlin jar on the host (sidestepping Docker's protoc sandbox),
# populates ./app-deps/ with resolved jars, then brings up ollama + piper +
# rpi-assistant + linux-voice-assistant.
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

# Wait for Ollama to answer the tags endpoint. First-boot is slow on a Pi
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

LOG "0/5  Building Kotlin jar on the host (skip Docker's protoc sandbox)"
# Reason: protoc downloads from Maven Central fail inside Docker's BuildKit
# network namespace on some hosts (incl. the Pi). Building here on the host
# means `docker compose build` only needs to copy artifacts.
"$ROOT/kotlin" task :pbandk-id-codegen:jarJvm
"$ROOT/kotlin" task :rpi-assistant:jarJvm

# Collect resolved dep jars so the Dockerfile COPY can pick them up. Amper
# caches differ per host; cover Linux Pi + macOS dev.
mkdir -p "$ROOT/app-deps"
DEPS_SRC=""
for cand in /root/.m2 "$HOME/.m2" "$HOME/.m2.cache" "$HOME/Library/Caches/JetBrains/Kotlin/.m2.cache"; do
  [[ -d "$cand" ]] && DEPS_SRC="$cand" && break
done
if [[ -z "$DEPS_SRC" ]]; then
  DIE "could not locate Maven/Gradle cache; expected one of /root/.m2, ~/.m2, ~/.m2.cache, ~/Library/Caches/JetBrains/Kotlin/.m2.cache"
fi
LOG "    collecting *.jar deps from $DEPS_SRC (skipping sources/javadoc)"
find "$DEPS_SRC" -name '*.jar' -not -name '*sources.jar' -not -name '*javadoc.jar' \
    -exec cp {} "$ROOT/app-deps/" \; 2>/dev/null || true

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
