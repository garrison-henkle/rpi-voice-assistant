#!/usr/bin/env bash
# One-shot bluetooth pair + connect for a Sonos (or any A2DP sink) we want
# the Pi's PipeWire to surface as an audio sink.
#
# Why a single-script(1) wrapper: every separate `script -q -c bluetoothctl`
# invocation spins up a brand-new bluetoothctl session and ends up
# re-registering the agent (and burning ~6 s on startup). Four separate
# invocations = ~25 s of pure startup overhead before the discovery scan
# even begins - Sonos pairing mode times out after ~60 s. So we run the
# whole flow inside one PTY session and use bluetoothctl's --timeout flag
# to bound the discovery window.
set -euo pipefail

MAC="${1:-74:CA:60:A6:F3:F8}"
SCAN_SECONDS="${SCAN_SECONDS:-15}"

LOG()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
DIE()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v bluetoothctl >/dev/null || DIE "bluetoothctl missing; apt install bluez"
command -v wpctl >/dev/null || DIE "wireplumber missing"

LOG "Single-session flow on $MAC. Press the Sonos BT button NOW (LED blue flash)."
LOG "Discovery will run for ${SCAN_SECONDS}s starting in 1 s."

# All in one script(1) PTY so the agent stays registered between commands.
# - bluetoothctl starts with --agent NoInputNoOutput so the agent registers
#   before we ever issue any scan/pair command, avoiding the
#   'Failed to register agent object' race we hit earlier.
# - The trailing `exit 0` lets bluetoothctl terminate cleanly after the
#   pipeline runs to completion.
script -q -c "bluetoothctl --timeout ${SCAN_SECONDS} --agent NoInputNoOutput" /dev/null <<'INNER' 2>&1
power off
power on
default-agent
scan on
INNER

# Discover-scan happens after a short delay so the user has time to press
# the Sonos BT button right before scan actually begins.
# Pair + trust + connect in a fresh agent-aware session.
script -q -c "bluetoothctl --agent NoInputNoOutput" /dev/null <<COMMANDS 2>&1
pair $MAC
trust $MAC
connect $MAC
quit
COMMANDS

LOG "Final state"
wpctl status
echo
journalctl -u bluetooth --since "1 minute ago" --no-pager 2>/dev/null | tail -10 || true
echo
echo "Done. If wpctl still lists no Sonos sink, the wireplumber monitor.bluez"
echo "needs an explicit force-load (we have Docker side ready to test with"
echo "SAT_OUTPUT_DEVICE=pulse once a sink does appear)."
