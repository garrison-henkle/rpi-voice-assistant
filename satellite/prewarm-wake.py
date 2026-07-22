"""Pre-download the openwakeword wake-word model into the image.

The openwakeword package stores per-wake-word models under
`resources/models/`. They are NOT bundled with `pip install openwakeword`;
they are fetched lazily the first time `Model(...)` is constructed.

The on-disk filename depends on the inference framework:

  * `tflite`  → `hey_<word>_v0.1.tflite`     ← always published
  * `onnx`    → `hey_<word>_v0.1.onnx`       ← NOT in v0.5.1 release assets

We always bake the *tflite* file because it's the one that actually exists
on the GitHub release. The runtime then constructs the Model with
`inference_framework="tflite"`.

The model source is also re-written into a `.onnx` sibling in the package
folder so older framework=pinned code doesn't blow up, but the runtime
container itself uses tflite exclusively.
"""

import os
import sys
import urllib.request

# Importing `openwakeword.MODELS` would be easier, but it pulls in
# onnxruntime as a side effect. Touching `__init__.py` indirectly is also
# brittle across versions. So we hardcode the canonical URL pattern from
# dscripka/openWakeWord releases (verified against `v0.5.1` assets).
_GITHUB_RELEASE_TEMPLATE = (
    "https://github.com/dscripka/openWakeWord/releases/download/{tag}/{filename}"
)
_KNOWN_TAG = "v0.5.1"


def _wake_filename(wake_word: str, framework: str = "tflite") -> str:
    return f"{wake_word}_v0.1.{framework}"


def _expected_target(wake_word: str, framework: str = "tflite") -> str:
    """Where inside the openwakeword package the file should live."""
    import openwakeword  # only here so the import is local
    pkg_dir = os.path.dirname(os.path.abspath(openwakeword.__file__))
    return os.path.join(pkg_dir, "resources", "models", _wake_filename(wake_word, framework))


def _download(url: str, dest: str) -> bool:
    """Stream a URL down to `dest`. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length", "0") or 0)
            done = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
            print(f"  downloaded {dest}  ({done:,} bytes)", flush=True)
        return True
    except Exception as e:
        print(f"  download failed: {url} → {e}", flush=True)
        return False


def main() -> int:
    wake_word = os.environ.get("SAT_WAKE_TH_WORD", "hey_rhasspy")

    print(f"pre-warming openwakeword model: {wake_word}", flush=True)

    # 1. Bake the .tflite file (the one that actually exists upstream).
    tflite_target = _expected_target(wake_word, framework="tflite")
    tflite_url = _GITHUB_RELEASE_TEMPLATE.format(
        tag=_KNOWN_TAG,
        filename=_wake_filename(wake_word, framework="tflite"),
    )
    os.makedirs(os.path.dirname(tflite_target), exist_ok=True)
    if not os.path.exists(tflite_target):
        if not _download(tflite_url, tflite_target):
            print(f"FAILED to pre-warm wake model '{wake_word}'", flush=True)
            return 1
    else:
        print(f"  already present: {tflite_target}", flush=True)

    # 2. Also try to fetch the .onnx counterpart (best-effort). If it 404s,
    #    that's fine — runtime uses tflite.
    onnx_target = _expected_target(wake_word, framework="onnx")
    onnx_url = _GITHUB_RELEASE_TEMPLATE.format(
        tag=_KNOWN_TAG,
        filename=_wake_filename(wake_word, framework="onnx"),
    )
    _download(onnx_url, onnx_target)  # log success/failure and continue

    print("wake-word model ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


