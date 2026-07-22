"""
assistant-satellite: a single-process glues mic + wake + STT + HTTP chat + TTS playback.

Pipeline:
  openwakeword(scores audio) → on wake
    record until ~1 s of silence  (energy-based VAD)
      POST  WAV → faster-whisper /v1/audio/transcriptions  → text_in
      POST  text_in → rpi-assistant /chat                 → {text_out, audio_b64}
    play WAV (decoded audio_b64) via sounddevice

All configuration is via environment variables; defaults are tuned for the
8 GB Pi + LVA replacement stack in `docker-compose.yml`.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from typing import Optional

import numpy as np
import requests
import sounddevice as sd
from openwakeword.model import Model as WakeModel


# --------------------------------------------------------------------------- #
# Environment                                                                 #
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


ASST_BASE_URL       = _env("RPI_ASST_BASE_URL",   "http://rpi-assistant:6059")
WHISPER_BASE_URL    = _env("WHISPER_BASE_URL",    "http://faster-whisper:9000")
WAKE_TH_WORD        = _env("SAT_WAKE_TH_WORD",    "hey_rhasspy")
WAKE_THRESHOLD      = float(_env("SAT_WAKE_THRESHOLD", "0.5"))
SAMPLE_RATE         = int(_env("SAT_SAMPLE_RATE",  "16000"))
CHANNELS            = 1
VAD_RMS_QUIET       = int(_env("SAT_VAD_RMS_QUIET",   "200"))
VAD_QUIET_FRAMES    = int(_env("SAT_VAD_QUIET_FRAMES", "12"))   # 80 ms each
VAD_MAX_FRAMES      = int(_env("SAT_VAD_MAX_FRAMES",   "1250"))  # ~100 s safety cap
SPK_DEVICE          = _env("SAT_SPK_DEVICE", "default")
DEBUG               = _env("SAT_DEBUG", "0") == "1"
BLOCK_FRAMES        = int(_env("SAT_BLOCK_FRAMES", "1280"))   # 80 ms @ 16 kHz
POST_TIMEOUT_S      = float(_env("SAT_POST_TIMEOUT_S", "45"))


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("satellite")


# --------------------------------------------------------------------------- #
# Audio helpers                                                               #
# --------------------------------------------------------------------------- #
def pcm_to_wav_bytes(pcm_16k_mono: np.ndarray, target_rate_hz: int) -> bytes:
    """Wrap a numpy int16 mono array into a 16-bit PCM WAV in memory."""
    if pcm_16k_mono.dtype != np.int16:
        pcm_16k_mono = pcm_16k_mono.astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)  # int16
        w.setframerate(target_rate_hz)
        w.writeframes(pcm_16k_mono.tobytes())
    return buf.getvalue()


def wav_response_to_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Read a WAV response from our TTS service → (int16 mono ndarray, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        nchan = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        # Resample / convert to int16 only if needed. Piper returns int16, so
        # this branch is just defensive.
        raise RuntimeError(f"expected 16-bit PCM, got sampwidth={sw}")
    arr = np.frombuffer(raw, dtype=np.int16)
    if nchan > 1:
        arr = arr.reshape(-1, nchan).mean(axis=1).astype(np.int16)
    return arr, sr


def rms_level(block_int16: np.ndarray) -> float:
    if block_int16.size == 0:
        return 0.0
    f = block_int16.astype(np.float32)
    return float(np.sqrt(np.mean(f * f)))


# --------------------------------------------------------------------------- #
# Backend calls                                                               #
# --------------------------------------------------------------------------- #
def transcribe(wav_bytes: bytes) -> Optional[str]:
    """POST WAV to faster-whisper's /v1/audio/transcriptions; returns the text."""
    try:
        files = {"file": ("utt.wav", wav_bytes, "audio/wav")}
        # 'whisper-1' is the OpenAI-API sentinel; the server uses whatever
        # model it's currently loaded with (see WHISPER_MODEL env).
        data  = {"model": "whisper-1", "language": "en", "response_format": "json"}
        url   = f"{WHISPER_BASE_URL}/v1/audio/transcriptions"
        log.debug("STT POST %s (%d bytes)", url, len(wav_bytes))
        r = requests.post(url, files=files, data=data, timeout=POST_TIMEOUT_S)
        r.raise_for_status()
        body = r.json()
        return (body.get("text") or "").strip()
    except Exception as e:
        log.warning("STT failed: %s", e)
        return None


def chat(text_in: str) -> Optional[tuple[str, np.ndarray, int]]:
    """POST text_in to rpi-assistant /chat; returns (text_out, audio_pcm16, sr)."""
    try:
        url   = f"{ASST_BASE_URL}/chat"
        log.debug("CHAT POST %s text=%r", url, text_in)
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"text": text_in}),
            timeout=POST_TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
        text_out    = body.get("text") or ""
        audio_b64   = body.get("audio_b64") or ""
        if not audio_b64:
            log.warning("/chat returned no audio_b64")
            return (text_out, np.zeros(0, dtype=np.int16), 22050)
        wav_bytes   = base64.b64decode(audio_b64)
        audio_pcm, sr = wav_response_to_pcm(wav_bytes)
        return (text_out, audio_pcm, sr)
    except Exception as e:
        log.warning("CHAT failed: %s", e)
        return None


def play(audio_pcm: np.ndarray, sr: int) -> None:
    if audio_pcm.size == 0:
        return
    log.info("playback: %d samples @ %d Hz (%.2fs)", audio_pcm.size, sr, audio_pcm.size / sr)
    try:
        sd.play(audio_pcm, samplerate=sr, device=SPK_DEVICE)
        sd.wait()
    except Exception as e:
        log.warning("audio playback failed: %s", e)


# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #
def main() -> int:
    log.info("START  ASST=%s  WHISPER=%s  wake=%s@[%.2f]", ASST_BASE_URL, WHISPER_BASE_URL, WAKE_TH_WORD, WAKE_THRESHOLD)

    # Probe PulseAudio sanity early so we crash in the right place with a
    # useful message rather than at the first sd.InputStream() call.
    if not os.path.isdir(_env("XDG_RUNTIME_DIR", "/run/user/1000") + "/pulse"):
        log.warning(
            "PulseAudio socket not mounted at %s/pulse — is "
            "/run/user/$UID mounted into the container?",
            _env("XDG_RUNTIME_DIR", "/run/user/1000"),
        )

    # openWakeWord: load the requested model. We use the `tflite` framework
    # because openwakeword 0.6's v0.5.1 GitHub release publishes ONLY `.tflite`
    # files for the per-wake-word models — `.onnx` URLs 404. The prewarm step
    # already baked the right `.tflite` file into the openwakeword package dir.
    try:
        wake_model = WakeModel(wakeword_models=[WAKE_TH_WORD], inference_framework="tflite")
        log.info("wake model loaded: %s", WAKE_TH_WORD)
    except Exception as e:
        log.warning("could not load in-image wake model '%s'; retrying online: %s", WAKE_TH_WORD, e)
        try:
            wake_model = WakeModel(wakeword_models=[WAKE_TH_WORD], inference_framework="tflite", download_updates=True)
            log.info("wake model loaded (with download): %s", WAKE_TH_WORD)
        except Exception as e2:
            log.error("could not load wake model '%s': %s", WAKE_TH_WORD, e2)
            return 2

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            dtype="int16",
            channels=CHANNELS,
            device=None,  # default source
        ) as mic:
            log.info("listening for wake word ('%s') on default input …", WAKE_TH_WORD)

            # State: idle / recording (after wake)
            state = "idle"
            rec_buffer: list[np.ndarray] = []
            silence_count = 0

            while True:
                data, _overflow = mic.read(BLOCK_FRAMES)
                block = np.frombuffer(data, dtype=np.int16).copy()

                if state == "idle":
                    preds = wake_model.predict(block)
                    score = max(preds.values()) if preds else 0.0
                    if DEBUG:
                        log.debug("wake score=%.3f", score)
                    if score >= WAKE_THRESHOLD:
                        log.info("WAKE detected (score=%.3f)", score)
                        rec_buffer = [block]
                        silence_count = 0
                        state = "recording"
                else:
                    rec_buffer.append(block)
                    if rms_level(block) < VAD_RMS_QUIET:
                        silence_count += 1
                    else:
                        silence_count = 0
                    if (
                        silence_count >= VAD_QUIET_FRAMES
                        or len(rec_buffer) >= VAD_MAX_FRAMES
                    ):
                        log.info("end of utterance (frames=%d, silence_count=%d)", len(rec_buffer), silence_count)
                        captured = np.concatenate(rec_buffer) if rec_buffer else np.zeros(0, dtype=np.int16)
                        rec_buffer = []
                        state = "idle"
                        # STT + chat + playback in a worker so we don't block the
                        # wake detector while it is thinking.
                        threading.Thread(
                            target=_handle_utterance,
                            args=(captured,),
                            daemon=True,
                        ).start()

    except KeyboardInterrupt:
        log.info("SIGINT — clean shutdown")
    except sd.PortAudioError as e:
        log.error("PortAudio error: %s — is PulseAudio available?", e)
        return 3
    return 0


def _handle_utterance(pcm: np.ndarray) -> None:
    if pcm.size < int(SAMPLE_RATE * 0.3):
        log.info("utterance too short — discard")
        return
    wav_bytes = pcm_to_wav_bytes(pcm, SAMPLE_RATE)
    text_in = transcribe(wav_bytes)
    if not text_in:
        log.info("STT returned empty; will not call orchestrator")
        return
    log.info("USER: %s", text_in)
    result = chat(text_in)
    if not result:
        log.info("orchestrator unreachable, will not play back anything")
        return
    text_out, audio_pcm, sr = result
    log.info("ASSISTANT: %s", text_out)
    play(audio_pcm, sr)


if __name__ == "__main__":
    sys.exit(main())
