#!/bin/sh
# `/bin/sh -c` entrypoint for the piper container. Replaces piper1-gpl's
# upstream ENTRYPOINT so we (a) fetch the requested voice ourselves and
# (b) start the http_server with it.
#
# `piper.download_voices` ships an internal name -> URL map that lags
# behind the rhasspy/piper-voices Hugging Face repo. So for voices that
# were contributed after that bundle's last update (e.g.
# en_US-hfc_male-medium) we go straight to Hugging Face via curl.

set -e

VOICE="${PIPER_VOICE:-en_US-lessac-medium}"
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
DATA_DIR="/data"

mkdir -p "$DATA_DIR"

cleanup_partial() {
    # if a `curl`-time download was interrupted, the half-written file
    # might be a valid-but-tiny file that confuses piper. Nuke anything
    # under 256 bytes so the next restart re-downloads cleanly.
    find "$DATA_DIR" -maxdepth 1 -name "${VOICE}.*" -size -256c -delete
}

echo "piper: requested voice = ${VOICE}"

case "$VOICE" in
    en_US-hfc_male-medium)
        BASE="${HF_BASE}/en/en_US/hfc_male/medium"
        cleanup_partial
        curl -fSL --retry 3 -o "${DATA_DIR}/${VOICE}.onnx"      "${BASE}/${VOICE}.onnx"
        curl -fSL --retry 3 -o "${DATA_DIR}/${VOICE}.onnx.json"  "${BASE}/${VOICE}.onnx.json"
        ;;
    *)
        # try the bundled voice-url map first (no network needed for popular voices)
        if ! python3 -m piper.download_voices "$VOICE" --data-dir "$DATA_DIR"; then
            BASE="${HF_BASE}/en/en_US/${VOICE#en_US-}"
            cleanup_partial
            curl -fSL --retry 3 -o "${DATA_DIR}/${VOICE}.onnx" \
                "${BASE}/${VOICE}.onnx" \
                || { echo "no model at ${BASE}/${VOICE}.onnx" >&2; exit 1; }
            curl -fSL --retry 3 -o "${DATA_DIR}/${VOICE}.onnx.json" \
                "${BASE}/${VOICE}.onnx.json"
        fi
        ;;
esac

echo "piper: files in ${DATA_DIR}:"
ls -la "$DATA_DIR"

echo "piper: launching http_server with -m ${VOICE}"
exec python3 -m piper.http_server \
    --host 0.0.0.0 --port 5000 \
    -m "$VOICE" --data-dir "$DATA_DIR"
