FROM python:3.11-slim-bookworm

# sounddevice uses PortAudio → libportaudio2 on Debian.
# moonshine-voice needs libsndfile1 for its wav_io; ffmpeg for any
# compressed audio we may need. alsa-utils ships `arecord`/`aplay`/`amixer`
# for repeatable in-container debugging of the device-routing layer (libasound2
# itself comes in transitively with libportaudio2 — we only add the CLI tools).
# libasound2-plugin-bluez lets us route the satellite's PCM output through
# the host's bluealsa service when SAT_OUTPUT_DEVICE=bluealsa. The plugin
# registers its .so under /usr/lib/aarch64-linux-gnu/alsa-lib/ but bookworm's
# build does NOT auto-extend the default PCM list; /etc/asound.conf has to
# declare the 'bluealsa' PCM name explicitly. We install /etc/asound.conf
# in a separate RUN below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libasound2-plugin-bluez \
        libsndfile1 \
        ffmpeg \
        alsa-utils \
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

# Bind the libasound2-plugin-bluez virtual PCM `bluealsa` so aplay/sounddevice
# can pick it up by name. The host's bluealsa service (system-wide D-Bus) is
# the audio bridge; this stanza just tells the ALSA side how to address it.
# We hard-code the Mac as a build-time placeholder; docker-compose will mount
# /etc/asound.conf in via a future PR so the address can change per room.
RUN printf '%s\n' \
        'pcm.!bluealsa {' \
        '  type bluealsa' \
        '  device "74:CA:60:A6:F3:F8"' \
        '  profile "a2dp"' \
        '}' \
        'ctl.!bluealsa { type bluealsa }' \
        > /etc/asound.conf

# PulseAudio socket and audio devices are mounted at runtime by docker-compose.
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "satellite.py"]
