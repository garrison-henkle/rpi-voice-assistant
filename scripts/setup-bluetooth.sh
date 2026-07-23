#!/usr/bin/env bash
# One-shot bluetooth pair + connect for a Sonos (or any A2DP sink) we want
# the Pi's PipeWire to surface as an audio sink.
#
# Why this script is structured this way:
# 1. Every separate `script -q -c bluetoothctl` invocation spins up a fresh
#    Bluetooth session and burns ~6 s on agent registration; with three of
#    those we'd eat the Sonos pairing window before any discovery happened.
# 2. bluetoothctl needs a TTY (for line editing and history), so we keep it
#    inside a `script(1)` PTY. We supply commands by piping them through
#    bash's process substitution so the pipe stays open long enough for
#    bluetoothctl to consume them in order.
# 3. Each command is followed by `sleep N` -- without that all five commands
#    land on bluetoothctl's stdin within milliseconds and it tries to parse
#    the "sleep" as a bluetoothctl command.
set -euo pipefail

MAC="${1:-74:CA:60:A6:F3:F8}"
LE_MAC="${2:-50:CE:C9:AB:94:72}"
SCAN_SECONDS="${SCAN_SECONDS:-25}"

LOG()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
DIE()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v bluetoothctl >/dev/null || DIE "bluetoothctl missing; apt install bluez"
command -v wpctl >/dev/null || DIE "wireplumber missing"

LOG "Press the Sonos BT button when you see this prompt -- the script starts scan immediately after."
sleep 1
# Use DisplayYesNo so Sonos gets a real SSP exchange -- some Sonos models
# reject NoInputNoOutput because they advertise a confirmation capability
# even though the user-action is just 'confirm' (no actual PIN).
AGENT_CAP="${AGENT_CAP:-DisplayYesNo}"

LOG "Single-session bluetoothctl (scan ${SCAN_SECONDS}s, then pair/trust/connect on $MAC with $AGENT_CAP agent)"

# Build a command stream that drives a single bluetoothctl session.
CMD_STREAM=$(
cat <<EOF
power on
default-agent
EOF
)

# Use a FIFO so the writer can sleep between commands without blocking on
# bluetoothctl reading the entire pipe (which would deadlock).
TMPFIFO=$(mktemp -u)
trap "rm -f $TMPFIFO" EXIT
mkfifo "$TMPFIFO"

# Reader: bluetoothctl in a PTY, reading from the FIFO.
( exec script -q -c "bluetoothctl --agent $AGENT_CAP" /dev/null ) < "$TMPFIFO" &
BTPID=$!

# Writer: write commands with sleeps.
exec 9> "$TMPFIFO"
{
  printf -- 'power on\n'
  sleep 1
  printf -- 'default-agent\n'
  sleep 1
  printf -- 'scan on\n'
  sleep "$SCAN_SECONDS"
  printf -- 'scan off\n'
  sleep 1
  printf -- "pair %s\n" "$MAC"
  sleep 2
  printf -- "trust %s\n" "$MAC"
  sleep 1
  printf -- "connect %s\n" "$MAC"
  sleep 3
  printf -- 'quit\n'
} >&9
exec 9>&-

wait $BTPID 2>/dev/null || true

LOG "Final state"
wpctl status
echo
sudo journalctl -u bluetooth --since "2 minutes ago" --no-pager 2>/dev/null | tail -10 || true
echo
echo "Done. If wpctl still lists no Sonos sink but the journal shows"
echo "'a2dp source profile connect succeeded' or 'media connected', the"
echo "wireplumber monitor.bluez is the bottleneck -- next step is to fix"
echo "that with RUST_LOG=debug or a manual monitor.bluez override."
