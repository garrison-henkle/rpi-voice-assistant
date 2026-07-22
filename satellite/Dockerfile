FROM python:3.11-slim-bookworm

# sounddevice uses PortAudio → libportaudio2 on Debian. openwakeword needs
# libsndfile1 for its wav_io, and ffmpeg for any compressed audio we may need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY satellite.py .

# PulseAudio socket and audio devices are mounted at runtime by docker-compose.
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "satellite.py"]
