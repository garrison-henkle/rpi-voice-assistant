#!/bin/sh
# `/bin/sh` entrypoint for the piper container. Replaces piper1-gpl's
# upstream ENTRYPOINT so we (a) fetch the requested voice ourselves and
# (b) start the http_server with it.
#
# `piper.download_voices` ships an internal name -> URL map that lags
# behind the rhasspy/piper-voices Hugging Face repo. So for voices that
# were contributed after that bundle's last update (e.g.
# en_US-hfc_male-medium) we go straight to Hugging Face via Python's
# stdlib urllib — we don't have `curl` or `wget` in piper1-gpl's image.

set -e

VOICE="${PIPER_VOICE:-en_US-lessac-medium}"
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
DATA_DIR="/data"

mkdir -p "$DATA_DIR"

# Stream `url` down to `dest`. Atomically rename from a `.part` file only
# when the result is at least 1 KiB, so a half-broken download can't
# leave a zero-byte file at the path Piper expects.
download_via_python() {
    url="$1"
    dest="$2"
    python3 - "$url" "$dest" <<'PY'
import os, sys, urllib.request

url, dest = sys.argv[1], sys.argv[2]
tmp = dest + ".part"
try:
    if os.path.exists(tmp):
        os.remove(tmp)
    with urllib.request.urlopen(url, timeout=180) as r:
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    if os.path.getsize(tmp) < 1024:
        raise RuntimeError(
            f"payload {os.path.getsize(tmp)} B is too small (likely 404 or empty body)"
        )
    os.replace(tmp, dest)
    print(f"  downloaded {dest} ({os.path.getsize(dest):,} B)")
except Exception as e:
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"  download failed: {url} -> {e}", file=sys.stderr)
    sys.exit(1)
PY
}

echo "piper: requested voice = ${VOICE}"

case "$VOICE" in
    en_US-hfc_male-medium)
        BASE="${HF_BASE}/en/en_US/hfc_male/medium"
        download_via_python "${BASE}/${VOICE}.onnx"     "${DATA_DIR}/${VOICE}.onnx"
        download_via_python "${BASE}/${VOICE}.onnx.json" "${DATA_DIR}/${VOICE}.onnx.json"
        ;;
    *)
        # try the bundled voice-url map first (no network needed for popular voices)
        if ! python3 -m piper.download_voices "$VOICE" --data-dir "$DATA_DIR"; then
            BASE="${HF_BASE}/en/en_US/${VOICE#en_US-}"
            download_via_python "${BASE}/${VOICE}.onnx" "${DATA_DIR}/${VOICE}.onnx"
            download_via_python "${BASE}/${VOICE}.onnx.json" "${DATA_DIR}/${VOICE}.onnx.json"
        fi
        ;;
esac

echo "piper: files in ${DATA_DIR}:"
ls -la "$DATA_DIR"

echo "piper: launching http_server with -m ${VOICE}"
exec python3 -m piper.http_server \
    --host 0.0.0.0 --port 5000 \
    -m "$VOICE" --data-dir "$DATA_DIR"
