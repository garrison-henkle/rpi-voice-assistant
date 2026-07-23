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
INPUT_DEVICE_RAW    = _env("SAT_INPUT_DEVICE", "")           # "" → auto-pick
OUTPUT_DEVICE_RAW   = _env("SAT_OUTPUT_DEVICE", "")          # "" → auto-pick
DEBUG               = _env("SAT_DEBUG", "0") == "1"
BLOCK_FRAMES        = int(_env("SAT_BLOCK_FRAMES", "1280"))   # 80 ms @ 16 kHz
POST_TIMEOUT_S      = float(_env("SAT_POST_TIMEOUT_S", "60"))
SPK_SAMPLE_RATE     = int(_env("SPK_SAMPLE_RATE",   "22050"))  # Piper output rate
SPK_CHUNK_MS        = int(_env("SPK_CHUNK_MS",      "80"))     # output block size


# --------------------------------------------------------------------------- #
# Audio device probing + auto-selection                                       #
# --------------------------------------------------------------------------- #
# PortAudio lists different views of the same hardware depending on whether
# the host ALSA plugin enumerates capture subdevices or whether pipewire/pulse
# routes through. We log the table at boot so mis-detection is visible, and
# we let `SAT_INPUT_DEVICE` / `SAT_OUTPUT_DEVICE` either name a device by id,
# name fragment, or one of the special tokens ``"default"`` / ``"pulse"`` /
# ``"alsa"`` to force a host API. Anything else leaves us on auto-detection:
# prefer the first device whose ``max_input_channels > 0`` at 16 kHz.


def _device_table() -> list[dict]:
    """Snapshot of every PortAudio device + whether it supports 16 kHz mono."""
    table: list[dict] = []
    for i, d in enumerate(sd.query_devices()):
        sr_ok = False
        try:
            sd.check_input_settings(
                device=i, samplerate=SAMPLE_RATE, channels=1, dtype="int16"
            )
            sr_ok = True
        except Exception:
            # Not a 16kHz mono input — fine for output-only devices.
            try:
                sd.check_output_settings(
                    device=i, samplerate=SAMPLE_RATE, channels=1, dtype="int16"
                )
                sr_ok = True
            except Exception:
                sr_ok = False
        table.append({
            "id": i,
            "name": d["name"],
            "host_api": sd.query_hostapis()[d["hostapi"]]["name"],
            "in": d["max_input_channels"],
            "out": d["max_output_channels"],
            "sr16k_ok": sr_ok,
        })
    return table


def _log_device_table() -> None:
    log.info("PortAudio device table:")
    for d in _device_table():
        marker = "*" if (d["in"] > 0 and d["sr16k_ok"]) else " "
        log.info(" %s %2d  in=%-3d out=%-3d 16k_ok=%-5s  api=%-12s  %r",
                 marker, d["id"], d["in"], d["out"], d["sr16k_ok"],
                 d["host_api"], d["name"])


def _parse_overrides() -> tuple[object, object]:
    """Parse SAT_INPUT_DEVICE / SAT_OUTPUT_DEVICE into (input, output) picks.

    Accepts either an integer id (PortAudio lists it), a substring of the
    device name (matched case-insensitively), or one of the special tokens:
    - ``default`` / ``pulse`` / ``alsa`` — host-API selectors PortAudio accepts
    - ``bluealsa`` — virtual PCM from libasound2-plugin-bluez; the plugin
      requires /etc/asound.conf to define it and the host's bluealsa service
      running with the right BT sink paired. We pass it through to
      ``sounddevice.RawOutputStream(device=…)`` because PortAudio does not
      enumerate plugin-based virtual PCMs in query_devices().
    - ``auto`` (or empty) — explicit sentinel meaning "let pick_*_device
      decide"; we use this both so that an empty env-var still means
      "intelligent fallback" and so the user can spell the same intent.
    Returns a tuple (input_override, output_override), each either a concrete
    int, a string, or ``None`` to fall through to the smart auto-detector.
    """
    def _one(raw: str, kind: str) -> object:
        s = raw.strip()
        if not s:
            return None  # empty -> smart auto
        if s.isdigit():
            return int(s)
        low = s.lower()
        if low in ("auto",):
            return None  # explicit "auto" -> smart auto
        if low in ("default", "pulse", "alsa", "bluealsa"):
            return s  # recognised special token, pass through
        # Substring match against the table
        for d in _device_table():
            if s.lower() in d["name"].lower():
                if kind == "input" and d["in"] > 0:
                    return d["id"]
                if kind == "output" and d["out"] > 0:
                    return d["id"]
        return s  # let sounddevice try — it'll raise a clearer error.

    return _one(INPUT_DEVICE_RAW, "input"), _one(OUTPUT_DEVICE_RAW, "output")


def _bluealsa_alive() -> bool:
    """Probe whether the libasound2-plugin-bluez 'bluealsa' PCM is reachable.

    We do a ``check_output_settings`` rather than opening and starting a
    stream because PortAudio's check is read-only. If bluealsa is alive AND
    a Sonos (or any BT sink with the configured MAC) is paired + connected,
    the probe succeeds; otherwise it raises.

    The probe is also free in the sense that the underlying transport is
    transient (pipewire wireplumber converts anything via a dummy sink) so we
    don't leak a stream handle even on success.
    """
    try:
        sd.check_output_settings(
            device="bluealsa", samplerate=48000, channels=2, dtype="int16"
        )
        return True
    except Exception:
        return False


def _accepts_speaker_sr(device) -> bool:
    """Probe whether ``device`` accepts the speaker sample rate we need.

    Falls back to a host-API plug wrapper (``plug:<id>``, ``plughw:<id>``,
    ``default``) when the picked device itself is a raw ``hw:`` PCM that
    doesn't enumerate the rate Piper emits. Plug sample-rate conversion is
    transparent to the user; volume and routing are unchanged.
    """
    sr = SPK_SAMPLE_RATE
    candidates: list[object] = [device]
    if isinstance(device, int):
        candidates.extend([f"plug:{device}", "default", "plug:default"])
    elif isinstance(device, str):
        if device.startswith(("plughw:", "plug:")):
            pass
        else:
            candidates.extend([f"plug:{device}", "default", "plug:default"])
        if device.startswith("hw:"):
            candidates.append("plughw:" + device[len("hw:"):])
    for c in candidates:
        try:
            sd.check_output_settings(device=c, samplerate=sr, channels=1, dtype="int16")
            return True
        except Exception:
            continue
    return False


def pick_input_device() -> object:
    """Return the PortAudio device id (or string) we should open for capture."""
    override, _ = _parse_overrides()
    if override is not None:
        log.info("input device override: %r", override)
        return override
    table = _device_table()
    for d in table:
        if d["in"] > 0 and d["sr16k_ok"]:
            log.info("auto-picked input device id=%d %r", d["id"], d["name"])
            return d["id"]
    # Fall back to "default" — sounddevice will raise a clearer message.
    log.warning("no 16 kHz capture device visible; falling back to 'default'")
    return "default"


def pick_output_device() -> object:
    _input_ignored, override = _parse_overrides()
    if override is not None:
        log.info("output device override: %r", override)
        return override

    # Smart auto: prefer BT (bluealsa) when a sink is paired on the host
    # so that moving the satellite to a wired speaker only requires turning
    # BT off — no env var edit. We probe bluealsa first; on failure we fall
    # back to the first device that accepts the Piper output SR via a
    # plug wrapper (resampling transparently), and only then to the raw
    # 16-kHz-capable id when nothing else works.
    if _bluealsa_alive():
        # Verify bluealsa accepts our Piper rate. With our container's plug
        # this always does, but other hosts (e.g. bluealsa-alsa-bridge on
        # a flat /etc/asound.conf) may not.
        if _accepts_speaker_sr("bluealsa"):
            log.info("auto-picked output device: 'bluealsa' (BT sink alive)")
            return "bluealsa"
        log.info("bluealsa probe OK but it rejects SR=%d; looking elsewhere",
                 SPK_SAMPLE_RATE)
    log.info("bluealsa unavailable; falling back to first 16 kHz stereo device")
    table = _device_table()
    # First pass: prefer plug-wrapped virtual PCMs (front, default, …) so
    # ALSA converts 22050 -> 48000 transparently. These always work
    # regardless of the device's native rate.
    plug_names = ("front", "surround40", "surround51", "surround71",
                  "iec958", "spdif", "dmix")
    for d in table:
        if d["out"] > 0 and d["sr16k_ok"]:
            if any(p in d["name"] for p in plug_names) or d["name"] in plug_names:
                wrapped = f"plug:{d['id']}"
                if _accepts_speaker_sr(wrapped):
                    log.info("auto-picked output device %r (plug wrapper over id=%d %r)",
                             wrapped, d["id"], d["name"])
                    return wrapped
    # Second pass: probe every candidate with the actual Piper rate via the
    # plug wrapper; pick the first that accepts. This is what made 22050
    # playback start working on reSpeaker, which only natively does 48k.
    for d in table:
        if d["out"] > 0 and d["sr16k_ok"]:
            wrapped = f"plug:{d['id']}"
            if _accepts_speaker_sr(wrapped):
                log.info("auto-picked output device %r (plug wrapper over id=%d %r)",
                         wrapped, d["id"], d["name"])
                return wrapped
    # Last resort: accept raw hw and pray ALSA converts.
    for d in table:
        if d["out"] > 0 and d["sr16k_ok"]:
            log.warning("no device accepted SR=%d plug; falling back to id=%d %r "
                        "(playback may refuse to start)",
                        SPK_SAMPLE_RATE, d["id"], d["name"])
            return d["id"]
    log.warning("no 16 kHz output device visible; falling back to 'default'")
    return "default"


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

    The stream is opened lazily on the first ``feed()`` so a host that has
    no speakers plugged in at boot (typical for headless Pi configs) does
    not crash the satellite before the user attaches one.
    """

    def __init__(
        self,
        q: "queue.Queue[Optional[np.ndarray]]",
        sr: int = SPK_SAMPLE_RATE,
        block_ms: int = SPK_CHUNK_MS,
        device: object = None,
    ):
        self._q = q
        sr_run = sr
        block = int(sr_run * block_ms / 1000)
        self._sr = sr_run
        self._block_frames = block
        self._zero = np.zeros(block, dtype=np.int16)
        self._current: Optional[np.ndarray] = None
        self._offset = 0
        self._closed = False
        self._stream: Optional[sd.RawOutputStream] = None
        self._open_attempts = 0
        self._device = device if device is not None else SPK_DEVICE

    def _ensure_open(self) -> bool:
        if self._stream is not None and not self._closed:
            return True
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self._sr,
                blocksize=self._block_frames,
                channels=CHANNELS,
                dtype="int16",
                device=self._device,
                callback=lambda out, _f, _t, _s: self._on_audio(out),
            )
            self._stream.start()
            log.info("output audio stream opened (sr=%d, block=%d, device=%r)",
                     self._sr, self._block_frames, self._device)
            return True
        except Exception as e:
            self._stream = None
            self._open_attempts += 1
            if self._open_attempts == 1 or self._open_attempts % 12 == 0:
                log.warning("output audio stream not available yet (attempt %d, device=%r): %s",
                            self._open_attempts, self._device, e)
            return False

    def __enter__(self) -> "ChunkPlayer":
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception as e:
            log.warning("audio stream close failed: %s", e)

    def feed(self, chunk_int16: np.ndarray) -> None:
        """Push one int16 chunk to the queue; open the audio device if needed."""
        if chunk_int16 is None or chunk_int16.size == 0:
            return
        # Lazily open so a host with no speakers at boot does not crash.
        self._ensure_open()
        try:
            self._q.put_nowait(chunk_int16)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(chunk_int16)
            except queue.Full:
                log.warning("playback queue dropping chunk (%d bytes)", chunk_int16.size)

    def feed_eos(self) -> None:
        self._ensure_open()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _on_audio(self, outdata) -> None:
        remaining = outdata.shape[0]
        out = bytearray()
        zero = self._zero.tobytes()
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
                    if first_audio:
                        # The orchestrator's first audio chunk keeps its 44-byte
                        # RIFF/WAVE header so we can parse the sample rate /
                        # channel layout; every subsequent slice ships raw
                        # 16-bit mono PCM already header-stripped server-side.
                        arr, _sr = wav_to_pcm(wav)
                        first_audio = False
                    else:
                        # Strip a stray RIFF header defensively in case a server
                        # change reverts to per-chunk WAVs, then interpret the
                        # rest as raw int16 PCM.
                        body = strip_wav_header(wav)
                        if len(body) % 2:
                            log.warning("odd byte count in PCM chunk (%d); trimming", len(body))
                            body = body[:-1]
                        arr = np.frombuffer(body, dtype=np.int16)
                        if arr.size == 0:
                            continue
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

    # ---- audio devices -------------------------------------------------- #
    # Print the full PortAudio device table so mis-detection is visible at
    # boot, then pick capture + playback devices honoring SAT_INPUT_DEVICE /
    # SAT_OUTPUT_DEVICE overrides. Auto-detection prefers the first device
    # whose max_input_channels > 0 and reports 16 kHz int16 mono support.
    _log_device_table()
    in_dev = pick_input_device()
    out_dev = pick_output_device()

    if not os.path.isdir(_env("XDG_RUNTIME_DIR", "/run/user/1000") + "/pulse"):
        log.warning(
            "PulseAudio socket not mounted at %s/pulse — playback may fall back to ALSA.",
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
    player = ChunkPlayer(out_q, device=out_dev)
    # Audio device opens lazily on the first `feed().` so a headless Pi
    # with no speakers at boot still runs without crashing.

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            dtype="int16",
            channels=CHANNELS,
            device=in_dev,
        ) as mic:
            log.info("listening for wake word ('%s') on input device=%r …", WAKE_TH_WORD, in_dev)
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
    if DEBUG:
        log.debug(
            "moonshine transcript dump: lines=%d, raw=%s",
            len(tr.lines),
            [(ln.text, [w.word if w is not None else None for w in (ln.words or [])]) for ln in tr.lines],
        )
    text_lines = [ln.text for ln in tr.lines if ln.text]
    text_in = " ".join(text_lines).strip()
    if not text_in:
        log.info("STT returned empty; will not call orchestrator")
        return
    stream_chat(text_in, transcriber, player, out_q)


if __name__ == "__main__":
    sys.exit(main())
