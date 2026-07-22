"""Pre-download the moonshine ASR ONNX model into the image.

`moonshine-voice` exposes `get_model_for_language("en", arch)` which
auto-downloads the specified checkpoint and returns (model_path, arch).
We pin MEDIUM_STREAMING for English (~450 MB on disk): the Pi 5 has 8 GB
of RAM, so 0.5 GB for the streaming STT is well within budget alongside
the Ollama model load + Piper.

Downloaded once at image-build time so the runtime container doesn't
need internet access on first boot and doesn't pay the ~30 s fetch cost
on every container start.
"""

import os
import sys


def main() -> int:
    print("pre-warming moonshine ASR model (en, MEDIUM_STREAMING)", flush=True)
    from moonshine_voice import ModelArch, get_model_for_language
    model_path, model_arch = get_model_for_language(
        wanted_language="en",
        wanted_model_arch=ModelArch.MEDIUM_STREAMING,
    )
    print(f"  arch: {model_arch.name} ({int(model_arch)})", flush=True)
    print(f"  path: {model_path}", flush=True)
    parent = os.path.dirname(model_path)
    total_bytes = 0
    file_count = 0
    for _root, _dirs, files in os.walk(parent):
        for f in files:
            p = os.path.join(_root, f)
            sz = os.path.getsize(p)
            total_bytes += sz
            file_count += 1
            print(f"    {p}  ({sz:,} bytes)", flush=True)
    print(
        f"moonshine model ready, {file_count} files, {total_bytes:,} bytes total",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
