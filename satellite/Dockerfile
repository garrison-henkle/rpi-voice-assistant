FROM python:3.11-slim-bookworm

# sounddevice uses PortAudio → libportaudio2 on Debian.
# moonshine-voice needs libsndfile1 for its wav_io; ffmpeg for any
# compressed audio we may need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the openwakeword per-wake-word model into the image so runtime
# doesn't need GitHub access on first boot. openwakeword ships the .tflite
# classifier for `hey_rhasspy` (and other wake words) on the v0.5.1 release.
COPY prewarm-wake.py /app/prewarm-wake.py
RUN SAT_WAKE_TH_WORD=hey_rhasspy python3 /app/prewarm-wake.py && \
    rm /app/prewarm-wake.py

# Same idea for moonshine/base — download at build time using the package's
# own helper so we ship an ONNX model verbatim. ~120 MB total.
COPY prewarm-moonshine.py /app/prewarm-moonshine.py
RUN python3 /app/prewarm-moonshine.py && \
    rm /app/prewarm-moonshine.py

COPY satellite.py .

# PulseAudio socket and audio devices are mounted at runtime by docker-compose.
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "satellite.py"]
