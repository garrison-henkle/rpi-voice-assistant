"""Pre-download the openwakeword wake-word model into the image.

The openwakeword package stores per-wake-word models under
`resources/models/`. They are NOT bundled with `pip install openwakeword`; they
are fetched lazily the first time `Model(...)` is constructed.

openwakeword 0.6 publishes model files on GitHub releases. v0.5.1 ships BOTH
`.tflite` files AND `.onnx` for the shared feature models
(`melspectrogram`, `embedding_model`, `silero_vad`), but only `.tflite`
variants for the per-wake-word classifiers (`hey_<word>_v0.1.tflite`).
Asking openwakeword to instantiate a wake model with
`inference_framework="onnx"` therefore 404s on the `.onnx` URL.

We bake the `.tflite` file (the one that actually exists in v0.5.1) and
leave runtime configured with `inference_framework="tflite"`.
"""

import os
import sys
import urllib.request


# Canonical upstream URL — verified against dscripka/openWakeWord @ v0.5.1
# release assets. Lower case tags are what's actually published.
_GITHUB_RELEASE_TEMPLATE = (
    "https://github.com/dscripka/openWakeWord/releases/download/{tag}/{filename}"
)
_KNOWN_TAG = "v0.5.1"


def _resolve_pkg_dir() -> str:
    """Find openwakeword's package dir without importing the package
    (we want to avoid transitively pulling tflite/onnx runtimes just to ask
    for a path)."""
    # Python's site-packages location for stdlib-image installs is well-known
    # and stable across Debian-slim / python:* images we use.
    probe = (
        os.environ.get("OPENWAKEWORD_PKG_DIR")
        or "/usr/local/lib/python3.11/site-packages/openwakeword"
        or "/usr/local/lib/python3.12/site-packages/openwakeword"
    )
    return probe


def _wake_filename(wake_word: str, framework: str) -> str:
    return f"{wake_word}_v0.1.{framework}"


def _expected_target(pkg_dir: str, wake_word: str, framework: str) -> str:
    return os.path.join(pkg_dir, "resources", "models", _wake_filename(wake_word, framework))


def _download(url: str, dest: str, min_bytes: int = 1024) -> bool:
    """Stream `url` down to `dest`. Atomically rename from a `.part` file
    only when the resulting payload is at least `min_bytes` long, so a
    half-broken download can never leave a zero-byte file behind.
    Returns True iff the file at `dest` is now valid."""
    tmp = dest + ".part"
    try:
        # nukes any stale partial file from a prior failed attempt
        if os.path.exists(tmp):
            os.remove(tmp)
        with urllib.request.urlopen(url, timeout=120) as r:
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        size = os.path.getsize(tmp)
        if size < min_bytes:
            raise RuntimeError(
                f"downloaded payload is only {size} bytes (expected >= {min_bytes}); "
                "likely a 200-on-an-empty-body / CDN bug or 404)"
            )
        os.replace(tmp, dest)
        print(f"  downloaded {dest}  ({size:,} bytes)", flush=True)
        return True
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"  download failed: {url} → {e}", flush=True)
        return False


def main() -> int:
    wake_word = os.environ.get("SAT_WAKE_TH_WORD", "hey_rhasspy")
    pkg_dir = _resolve_pkg_dir()

    print(f"pre-warming openwakeword model: {wake_word}", flush=True)
    print(f"  package: {pkg_dir}", flush=True)

    if not os.path.isdir(pkg_dir):
        print(
            f"WARNING: openwakeword package dir not found at {pkg_dir}; skipping bake "
            "(runtime will need a network attempt to fetch against its own MODELS map).",
            flush=True,
        )
        return 0  # do not fail the build — the satellite's runtime can still try to download.

    # 1. The .tflite file (the one publish in v0.5.1).
    tflite_target = _expected_target(pkg_dir, wake_word, "tflite")
    if os.path.exists(tflite_target) and os.path.getsize(tflite_target) >= 1024:
        print(f"  already present (>= 1KiB): {tflite_target}", flush=True)
    else:
        if os.path.exists(tflite_target):
            print(
                f"  existed but suspiciously small ({os.path.getsize(tflite_target)} B); "
                "re-downloading",
                flush=True,
            )
            os.remove(tflite_target)
        tflite_url = _GITHUB_RELEASE_TEMPLATE.format(
            tag=_KNOWN_TAG,
            filename=_wake_filename(wake_word, "tflite"),
        )
        if not _download(tflite_url, tflite_target):
            print("FAILED to pre-warm wake model (.tflite)", flush=True)
            return 1

    # 2. Best-effort attempt for the .onnx counterpart. If it 404s we don't
    #    care — runtime uses tflite. We just record success/failure for the log.
    onnx_target = _expected_target(pkg_dir, wake_word, "onnx")
    if not os.path.exists(onnx_target) or os.path.getsize(onnx_target) < 1024:
        if os.path.exists(onnx_target):
            os.remove(onnx_target)
        onnx_url = _GITHUB_RELEASE_TEMPLATE.format(
            tag=_KNOWN_TAG,
            filename=_wake_filename(wake_word, "onnx"),
        )
        _download(onnx_url, onnx_target)  # log only

    print("wake-word model ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


