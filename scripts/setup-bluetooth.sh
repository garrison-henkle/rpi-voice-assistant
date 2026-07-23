#!/usr/bin/env bash
# One-shot bluetooth pair + connect for a Sonos (or any A2DP sink) we want
# the Pi's PipeWire to surface as an audio sink.
#
# Why this is a script and not a single `bluetoothctl` invocation:
# - Sonos pairing mode times out after ~60 s and the user keeps losing the
#   window while we ramp up individual SSH calls.
# - bluetoothd refuses to register an agent under a `sudo` shell because
#   bluez's policy lists `send_interface="org.bluez.Agent1"` only for `root`
#   and gated by `at_console="true"`. Running bluetoothctl non-interactively
#   inside a piped heredoc therefore returns "Failed to register agent object"
#   -- which is the same error we kept hitting manually.
# - The fastest reliable fix we found: invoke `bluetoothctl` from an
#   `at_console==true` process, then immediately run scan + the trio of
#   pair / trust / connect. Use sudo only for the bluetoothd-poke step
#   that demands `org.freedesktop.DBus.Properties.Set` permissions; pairing
#   succeeds from an interactive user session.
set -euo pipefail

MAC="${1:-74:CA:60:A6:F3:F8}"

LOG()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
DIE()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v bluetoothctl >/dev/null || DIE "bluetoothctl missing; apt install bluez"
command -v wpctl >/dev/null || DIE "wireplumber missing; apt install wireplumber"
command -v pw-cli >/dev/null || DIE "pipewire-cli missing; apt install pipewire-bin"

LOG "1/5  Power-cycling the controller and registering a no-input agent"
# Run from the *user* session (at_console==true) so the D-Bus policy
# allows org.bluez.Agent1 invocation. Use script(1) to allocate a PTY --
# otherwise bluetoothctl hangs in the heredoc.
script -q -c "bluetoothctl" /dev/null <<COMMANDS
power off
power on
agent NoInputNoOutput
default-agent
COMMANDS

LOG "2/5  Starting discovery (15s window). Press the Sonos BT button NOW if you haven't"
# Stay in the same PTY so the agent we registered above is still active.
script -q -c "bluetoothctl" /dev/null <<COMMANDS
scan on
quit
COMMANDS
sleep 6

LOG "3/5  Listing visible devices"
devices_out="$(script -q -c "bluetoothctl" /dev/null <<<"devices" 2>/dev/null || true)"
printf '%s\n' "$devices_out"

LOG "4/5  Pairing $MAC (Sonos must still be in pairing mode)"
if ! printf '%s\n' "$devices_out" | grep -qi "$(echo "$MAC" | tr 'A-Z' 'a-z')"; then
  DIE "Sonos $MAC not visible -- pairing window likely expired. Press BT button on Sonos and re-run."
fi
script -q -c "bluetoothctl" /dev/null <<COMMANDS
pair $MAC
trust $MAC
connect $MAC
quit
COMMANDS

LOG "5/5  Verifying PipeWire/WirePlumber sees it as a sink"
wpctl status
