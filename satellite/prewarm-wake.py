"""Pre-download openwakeword models into the image.

The pip-published openwakeword wheel does NOT ship:
  - The shared feature models (melspectrogram.{tflite,onnx}, embedding_model.{tflite,onnx})
  - The Silero VAD ONNX model
  - Per-wake-word classifiers (hey_rhasspy_v0.1.* etc.)
These are all fetched lazily the first time `Model(...)` constructs. We'd rather
bake them into the image so a Pi without internet access still boots.

openwakeword 0.6 ships an internal helper, `openwakeword.utils.download_models`,
which knows the official URLs by reading `MODELS`, `FEATURE_MODELS`, `VAD_MODELS`
in `openwakeword/__init__.py`. We just call that — much safer than guessing
file paths and release tags ourselves.
"""

import os
import sys


def main() -> int:
    # We don't actually need anything user-configurable; the wake-word we ask
    # for below is informational. The shared feature + VAD models are pulled
    # unconditionally because `Model(...)` always needs them.
    requested_wake_word = os.environ.get("SAT_WAKE_TH_WORD", "hey_rhasspy")

    print(f"pre-warming openwakeword models (wake word: {requested_wake_word})", flush=True)

    # Importing openwakeword has a side-effect of populating MODELS / FEATURE_MODELS
    # / VAD_MODELS dictionaries.
    import openwakeword
    from openwakeword.utils import download_models

    target_dir = os.path.join(
        os.path.dirname(os.path.abspath(openwakeword.__file__)),
        "resources", "models",
    )
    os.makedirs(target_dir, exist_ok=True)

    # `download_models` is idempotent: it skips any file already present. Pass
    # the wake word we care about so the per-wake-word classifier is also
    # fetched; the rest of MODELS gets downloaded unfortunately too, but the
    # extra ~2 MiB is a small price for "the image boots offline".
    download_models(
        model_names=[requested_wake_word],
        target_directory=target_dir,
    )

    # Surface what we ended up with so the Docker build log shows progress.
    print(f"  target dir: {target_dir}", flush=True)
    for entry in sorted(os.listdir(target_dir)):
        path = os.path.join(target_dir, entry)
        if os.path.isfile(path):
            print(f"    {entry}  ({os.path.getsize(path):,} bytes)", flush=True)

    print("wake-word models ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
