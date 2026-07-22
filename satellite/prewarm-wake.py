"""Pre-download openwakeword's pretrained wake-word model into the image.

The openwakeword package lazily fetches the `.onnx` file for the requested
wake-word the first time `Model(...)` is invoked. On a Pi that's both slow
and can fail if the network is restricted. Running this at image build time
guarantees the model is on disk before the satellite container first boots,
so runtime never has to wait.
"""

import os
import sys

from openwakeword.model import Model


def main() -> int:
    # Pick a wake-word model that's small + multilingual-friendly. The
    # default openwakeword zoo has the following valid wake-words documented
    # in the README:
    #   "alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy"
    # We default to "hey_rhasspy" (matches the satellite's expectation).
    wake_word = os.environ.get("SAT_WAKE_TH_WORD", "hey_rhasspy")

    print(f"pre-warming openwakeword model: {wake_word}", flush=True)
    try:
        # This call downloads the .onnx to openwakeword's package dir if it
        # isn't already cached locally.
        m = Model(wakeword_models=[wake_word], inference_framework="onnx")
        # Touch predict() so the lazy n_features/melspectrogram.onnx/etc.
        # pieces also come down. The first frame is computed against a real
        # buffer; any 1-second mono int16 array would also work — we just
        # need the side effect of model/feature load.
        _ = m.predict
        print("wake-word model ready", flush=True)
        return 0
    except Exception as e:
        print(f"FAILED to pre-warm wake model '{wake_word}': {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())


