"""
assistant-satellite: streaming voice pipeline.

Pipeline:
  openwakeword(scores audio) → on wake
    record until ~ end of utterance (RMS VAD)
      moonshine ASR (medoid streaming, MEDIUM_STREAMING)
      POST  text → rpi-assistant /chat/stream (NDJSON)
        text_delta events appended to a "buffer"
        audio_delta events decoded and handed to a queue
          RawOutputStream drainer plays queued PCM chunks
      end-of-utterance reset state

The mic and the LLM/TTS loop are decoupled: we don't wait for the LLM
to finish before opening the audio device; instead we let Piper's per-chunk
WAV bytes flow through a queue that RawOutputStream consumes in chunks of
~80 ms each.

All configuration is via environment variables; defaults are tuned for the
8 GB Pi 5 + moonshine-medium + Ollama + Piper1-gpl stack in
`docker-compose.yml`.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import queue
import sys
import threading
import time
import wave
from typing import Callable, Optional

import numpy as np
import requests
import sounddevice as sd
from moonshine_voice import ModelArch, get_model_for_language
from moonshine_voice.transcriber import Transcriber
from openwakeword.model import Model as WakeModel


# --------------------------------------------------------------------------- #
# Environment                                                                 #
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


ASST_BASE_URL       = _env("RPI_ASST_BASE_URL",   "http://rpi-assistant:6059")
WAKE_TH_WORD        = _env("SAT_WAKE_TH_WORD",    "hey_rhasspy")
WAKE_THRESHOLD      = float(_env("SAT_WAKE_THRESHOLD", "0.5"))
SAMPLE_RATE         = int(_env("SAT_SAMPLE_RATE",  "16000"))
CHANNELS            = 1
VAD_RMS_QUIET       = int(_env("SAT_VAD_RMS_QUIET",   "200"))
VAD_QUIET_FRAMES    = int(_env("SAT_VAD_QUIET_FRAMES", "12"))   # 80 ms each
VAD_MAX_FRAMES      = int(_env("SAT_VAD_MAX_FRAMES",   "1250"))  # 100 s safety cap
SPK_DEVICE          = _env("SAT_SPK_DEVICE", "default")
DEBUG               = _env("SAT_DEBUG", "0") == "1"
BLOCK_FRAMES        = int(_env("SAT_BLOCK_FRAMES", "1280"))   # 80 ms @ 16 kHz
POST_TIMEOUT_S      = float(_env("SAT_POST_TIMEOUT_S", "60"))
SPK_SAMPLE_RATE     = int(_env("SPK_SAMPLE_RATE",   "22050"))  # Piper output rate
SPK_CHUNK_MS        = int(_env("SPK_CHUNK_MS",      "80"))     # output block size


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
def pcm16_to_float32(pcm_int16: np.ndarray) -> np.ndarray:
    """Convert int16 PCM [-32768, 32767] to float32 [-1.0, 1.0]."""
    return pcm_int16.astype(np.float32) / 32768.0


def wav_to_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Read a WAV (16-bit little-endian PCM) into (int16 ndarray, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr = w.getframerate()
        nchan = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError(f"expected 16-bit PCM, got sampwidth={sw}")
    arr = np.frombuffer(raw, dtype=np.int16)
    if nchan > 1:
        arr = arr.reshape(-1, nchan).mean(axis=1).astype(np.int16)
    return arr, sr


def strip_wav_header(wav_bytes: bytes) -> bytes:
    """Drop the leading RIFF/WAVE header (44 bytes for 16-bit PCM mono).

    Piper emits a full valid WAV for every call — when piping multiple
    chunks back-to-back, only the first chunk should keep its header so the
    playback device parses pre-headers as unique streams. We strip the
    header from every subsequent chunk.
    """
    if len(wav_bytes) >= 44 and wav_bytes[:4] == b"RIFF":
        return wav_bytes[44:]
    return wav_bytes


def rms_level(block_int16: np.ndarray) -> float:
    if block_int16.size == 0:
        return 0.0
    f = block_int16.astype(np.float32)
    return float(np.sqrt(np.mean(f * f)))


# --------------------------------------------------------------------------- #
# Output playback                                                             #
# --------------------------------------------------------------------------- #
class ChunkPlayer:
    """Consumes individual int16 PCM chunks off a queue into a sounddevice stream.

    sounddevice's RawOutputStream pops block-sized slices out through a
    callback running on the audio thread; if the queue is empty the
    callback fills the buffer with zeros (silent).
    """

    def __init__(
        self,
        q: "queue.Queue[Optional[np.ndarray]]",
        sr: int = SPK_SAMPLE_RATE,
        block_ms: int = SPK_CHUNK_MS,
        on_underrun: Optional[Callable[[int], None]] = None,
    ):
        self._q = q
        sr_run = sr
        block = int(sr_run * block_ms / 1000)
        self._block_frames = block
        self._zero = np.zeros(block, dtype=np.int16)
        self._current: Optional[np.ndarray] = None
        self._offset = 0
        self._closed = False
        self._stream = sd.RawOutputStream(
            samplerate=sr_run,
            blocksize=block,
            channels=CHANNELS,
            dtype="int16",
            device=SPK_DEVICE,
            callback=lambda out, _f, _t, _s: self._on_audio(out),
        )

    def __enter__(self) -> "ChunkPlayer":
        self._stream.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            log.warning("audio stream close failed: %s", e)

    def feed(self, chunk_int16: np.ndarray) -> None:
        """Push one int16 chunk to the queue."""
        if chunk_int16 is None or chunk_int16.size == 0:
            return
        try:
            self._q.put_nowait(chunk_int16)
        except queue.Full:
            # Drop oldest to avoid backing up on slow playback paths.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(chunk_int16)
            except queue.Full:
                log.warning("playback queue dropping chunk (%d bytes)", chunk_int16.size)

    def feed_eos(self) -> None:
        """Signal end-of-stream; pump() will then drain to silence on the next pass."""
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _on_audio(self, outdata) -> None:
        """sounddevice raw-output callback: produce up to blocksize int16 samples."""
        remaining = outdata.shape[0]
        out = bytearray()
        zero = self._zero.tobytes()
        # If we have an unfinished current buffer, drain it first.
        while remaining > 0 and self._current is not None and self._offset < self._current.size:
            take = min(remaining, self._current.size - self._offset)
            out.extend(self._current[self._offset:self._offset + take].tobytes())
            self._offset += take
            remaining -= take
        while remaining > 0:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                item = None
            if item is None:
                # EOS signal — fall through to silence padding.
                self._current = None
                n_bytes = remaining * 2
                if n_bytes > len(zero):
                    out.extend(zero * ((n_bytes // len(zero)) + 1))
                else:
                    out.extend(zero[:n_bytes])
                remaining = 0
                break
            self._current = item
            self._offset = 0
            take = min(remaining, self._current.size)
            out.extend(self._current[:take].tobytes())
            self._offset = take
            remaining -= take
        view = memoryview(outdata).cast("B")
        if len(out) < len(view):
            out.extend(bytearray(len(view) - len(out)))
        view[: len(out)] = out


# --------------------------------------------------------------------------- #
# Moonshine ASR — initialized once per process                                #
# --------------------------------------------------------------------------- #
def _load_moonshine() -> tuple[Transcriber, ModelArch]:
    model_path, model_arch = get_model_for_language("en", ModelArch.MEDIUM_STREAMING)
    log.info("moonshine model loaded: arch=%s path=%s", model_arch.name, model_path)
    t = Transcriber(model_path=str(model_path), model_arch=model_arch)
    return t, model_arch


# --------------------------------------------------------------------------- #
# Streaming chat — chunked HTTP from /chat/stream                              #
# --------------------------------------------------------------------------- #
def stream_chat(
    text_in: str,
    transcriber: Transcriber,
    player: ChunkPlayer,
    q: "queue.Queue[Optional[np.ndarray]]",
) -> None:
    """POST text to /chat/stream, draining NDJSON into player + transcriber.

    On `{"type":"text_delta",...}` we accumulate incrementally.
    On `{"type":"audio_delta",...}` we decode base64 + strip the WAV
    header (after the very first chunk) and queue the int16 PCM.
    On `{"type":"done"}` we finalize.
    We also send the first chunk back into the transcriber as a
    "ghost transcript" forward through any audio resync logic (left as a
    TODO if/when echo cancellation arrives).
    """
    try:
        url = f"{ASST_BASE_URL}/chat/stream"
        log.info("USER: %s (POST %s)", text_in, url)
        text_buf: list[str] = []
        first_audio = True
        with requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"text": text_in}),
            timeout=POST_TIMEOUT_S,
            stream=True,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("bad NDJSON line from /chat/stream: %r (%s)", line, e)
                    continue
                kind = ev.get("type")
                if kind == "text_delta":
                    text_buf.append(ev.get("text", ""))
                elif kind == "audio_delta":
                    b64 = ev.get("data", "") or ""
                    if not b64:
                        continue
                    wav = base64.b64decode(b64)
                    pcm = strip_wav_header(wav) if not first_audio else wav
                    first_audio = False
                    arr, _sr = wav_to_pcm(pcm)  # already int16 mono
                    player.feed(arr)
                elif kind == "done":
                    break
                elif kind == "error":
                    log.warning("orchestrator error: %s", ev)
                    break
        log.info("ASSISTANT: %s", "".join(text_buf))
    except Exception as e:
        log.warning("CHAT failed: %s", e)
    finally:
        # Signal end-of-stream; pump() may still flush a final partial
        # block but will then drain to silence.
        player.feed_eos()


# --------------------------------------------------------------------------- #
# Main loop                                                                   #
# --------------------------------------------------------------------------- #
def main() -> int:
    log.info(
        "START  ASST=%s  wake=%s@[%.2f]  spk_sr=%d  block=%d frames",
        ASST_BASE_URL, WAKE_TH_WORD, WAKE_THRESHOLD, SPK_SAMPLE_RATE, BLOCK_FRAMES,
    )

    if not os.path.isdir(_env("XDG_RUNTIME_DIR", "/run/user/1000") + "/pulse"):
        log.warning(
            "PulseAudio socket not mounted at %s/pulse — playback may fail.",
            _env("XDG_RUNTIME_DIR", "/run/user/1000"),
        )

    # ---- wake model ----------------------------------------------------- #
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

    # ---- moonshine ------------------------------------------------------ #
    transcriber = _load_moonshine()[0]

    # ---- output audio queue + player ----------------------------------- #
    out_q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=64)
    player = ChunkPlayer(out_q)
    player.__enter__()

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            dtype="int16",
            channels=CHANNELS,
            device=None,
        ) as mic:
            log.info("listening for wake word ('%s') on default input …", WAKE_TH_WORD)

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
                        log.info(
                            "end of utterance (frames=%d, silence_count=%d)",
                            len(rec_buffer),
                            silence_count,
                        )
                        captured = np.concatenate(rec_buffer) if rec_buffer else np.zeros(0, dtype=np.int16)
                        rec_buffer = []
                        state = "idle"
                        threading.Thread(
                            target=_handle_utterance,
                            args=(captured, transcriber, out_q, player),
                            daemon=True,
                        ).start()

    except KeyboardInterrupt:
        log.info("SIGINT — clean shutdown")
    except sd.PortAudioError as e:
        log.error("PortAudio error: %s — is PulseAudio available?", e)
        return 3
    finally:
        try:
            player.close()
        except Exception:
            pass
    return 0


def _handle_utterance(
    pcm: np.ndarray,
    transcriber: Transcriber,
    out_q: "queue.Queue[Optional[np.ndarray]]",
    player: ChunkPlayer,
) -> None:
    if pcm.size < int(SAMPLE_RATE * 0.3):
        log.info("utterance too short — discard")
        return
    floats = pcm16_to_float32(pcm)
    try:
        tr = transcriber.transcribe_without_streaming(floats.tolist(), SAMPLE_RATE)
    except Exception as e:
        log.warning("moonshine transcribe failed: %s", e)
        return
    text_lines = [ln.words for ln in tr.lines if ln.words]
    text_in = " ".join(text_lines).strip()
    if not text_in:
        log.info("STT returned empty; will not call orchestrator")
        return
    stream_chat(text_in, transcriber, player, out_q)


if __name__ == "__main__":
    sys.exit(main())
