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
VAD_RMS_QUIET       = int(_env("SAT_VAD_RMS_QUIET",   "150"))
VAD_QUIET_FRAMES    = int(_env("SAT_VAD_QUIET_FRAMES", "12"))   # 80 ms each
VAD_MAX_FRAMES      = int(_env("SAT_VAD_MAX_FRAMES",   "480"))  # 38 s safety cap
# 1 (default) → push each captured 80 ms block into moonshine's
# `add_audio` + `update_transcription` loop while we are still recording,
# so by the time VAD fires the transcript is already final. 0 → keep the
# old offline `transcribe_without_streaming` path (useful when the
# MEDIUM_STREAMING model drifts on streamed audio for the user's accent).
SAT_STREAMING_ASR    = _env("SAT_STREAMING_ASR", "1") == "1"
# 1 → emit a local-arpeggio chime when /chat/stream reports a
# tool execution, replacing the empty `content` the LLM sometimes emits
# before the tool returns. 0 → no chime.
SAT_CHIME_ON_TOOL    = _env("SAT_CHIME_ON_TOOL", "1") == "1"
# 1 → run a small startup probe against ollama + piper to load weights
# and voice synth, so the first wake word doesn't pay a cold-load tax.
# 0 → skip pre-warm (useful if you're already pre-loading from systemd).
SAT_PREWARM          = _env("SAT_PREWARM",       "1") == "1"
# Probe URLs used by pre-warm. Defaults assume the docker bridge name
# from docker-compose.yml; sat-side overrides are mainly for ad-hoc tests.
OLLAMA_TAGS_URL      = _env("RPI_LLM_TAGS_URL",  "http://ollama:11434/api/tags")
PIPER_HEALTH_URL     = _env("RPI_TTS_HEALTH_URL", "http://piper:5000/info")
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

    Sounddevice won't dispatch raw device strings like ``plug:N`` /
    ``plughw:0,0`` (which is why those fall through with 'No matching
    device'); only entries PortAudio enumerated forward work. Use either
    the device id directly or one of the host-API aliases (``default``,
    ``sysdefault``, ``pulse``). ``default``/``sysdefault`` go through dmix
    and ALSA's plug plugins, which transparently resample 22050 -> 48000.
    """
    sr = SPK_SAMPLE_RATE
    try:
        sd.check_output_settings(device=device, samplerate=sr, channels=1, dtype="int16")
        return True
    except Exception:
        return False


# Host-API aliases that route through ALSA's plug/dmix and therefore
# transparently resample the piper 22 kHz stream to 48000. We probe these
# in priority order — they're the only sure-fire way to accept a rate
# the picked hw: PCM doesn't natively enumerate.
_RESAMPLE_PROBES = ("default", "sysdefault")


def _max_speaker_volume(out_dev) -> None:
    """Max out PCM controls via amixer on the chosen output device.

    The reSpeaker XVF3800 USB Audio gadget ships both PCM,0 (stereo) and
    PCM,1 (mono) at ~62% / ~67% from the factory; the mono control is
    independent and `alsactl init` does not raise it along with the
    stereo one. amixer absent or non-USB output is fine — the call is
    best-effort and never aborts boot.
    """
    if not (isinstance(out_dev, str) and out_dev in {"default", "sysdefault"}):
        log.debug("skip amixer volume bump for non-default output %r", out_dev)
        return
    import shutil
    import subprocess
    if shutil.which("amixer") is None:
        log.debug("amixer not on PATH; skipping volume bump")
        return
    for ctl in ("PCM,0", "PCM,1", "PCM"):
        try:
            r = subprocess.run(
                ["amixer", "sset", ctl, "100%"],
                capture_output=True, text=True, timeout=2,
            )
            log.info("amixer sset %s 100%%: rc=%d", ctl, r.returncode)
        except Exception as e:
            log.warning("amixer sset %s failed: %s", ctl, e)


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
    # back to ALSA's `default` / `sysdefault` host-API alias which routes
    # through dmix + the plug plugin so 22 kHz is transparently resampled
    # to whatever the speaker accepts. We deliberately avoid raw `hw:N`
    # ids (which are enumerated but reject 22050 Hz with paInvalidSampleRate)
    # because the playback stream opens silently and the user hears nothing.
    if _bluealsa_alive():
        if _accepts_speaker_sr("bluealsa"):
            log.info("auto-picked output device: 'bluealsa' (BT sink alive)")
            return "bluealsa"
        log.info("bluealsa probe OK but it rejects SR=%d; looking elsewhere",
                 SPK_SAMPLE_RATE)
    log.info("bluealsa unavailable; looking for a SR=%d-resampling host-API alias",
             SPK_SAMPLE_RATE)
    for probe in _RESAMPLE_PROBES:
        if _accepts_speaker_sr(probe):
            log.info("auto-picked output device: %r (resamples %d Hz)",
                     probe, SPK_SAMPLE_RATE)
            return probe
    # Last resort: pick the first device whose direct probe accepts 22050.
    # This typically returns 'sysdefault' or 'default' on a vanilla Pi; on
    # a host with neither it falls through to id=0 (a raw hw: PCM that
    # will fail to open the playback stream — the user will not hear
    # anything).
    table = _device_table()
    for d in table:
        if d["out"] > 0 and d["sr16k_ok"] and _accepts_speaker_sr(d["id"]):
            log.info("auto-picked output device id=%d %r (native SR match)",
                     d["id"], d["name"])
            return d["id"]
    log.warning("no device accepted SR=%d; falling back to 'default' "
                "(playback may still refuse to start)",
                SPK_SAMPLE_RATE)
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


# ---------------------------------------------------------------------------
# Tool-acknowledgement chime + boot pre-warm
# ---------------------------------------------------------------------------
# When the orchestrator decides to call a tool, qwen3 often leaves
# `message.content` empty so the user hears nothing alive until the tool
# result comes back. We synthesise a soft ascending arpeggio at boot and
# queue it onto the player queue the moment `{"type":"chime"}` arrives
# in the NDJSON stream. The cue is short (~480 ms), full-amplitude but
# ducked to -7 dB, so it doesn't fight the spoken ack that follows.

_CHIME_NOTES_HZ = (523.25, 659.25, 783.99)   # C5, E5, G5


def _synthesise_chime(
    note_ms: int = 80,
    sr: int = SPK_SAMPLE_RATE,
    peak: float = 0.45,
) -> np.ndarray:
    """Render the arpeggio to a single int16 PCM chunk.

    Each note has 5 ms fade in / fade out to kill the click; 5 ms gap
    between notes keeps the rhythm breathy rather than mechanical.
    """
    note_n = int(sr * note_ms / 1000)
    fade = max(2, int(sr * 0.005))
    pieces: list[np.ndarray] = []
    for hz in _CHIME_NOTES_HZ:
        t = np.arange(note_n, dtype=np.float32) / sr
        wave = np.sin(2 * np.pi * hz * t).astype(np.float32)
        env = np.ones(note_n, dtype=np.float32)
        env[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        env[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        pieces.append((wave * env * peak))
        # 5 ms silence between notes
        pieces.append(np.zeros(int(sr * 0.005), dtype=np.float32))
    pcm = np.concatenate(pieces).clip(-1.0, 1.0)
    return (pcm * 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# Wake-word mute + ASR lock (process-wide)
# ---------------------------------------------------------------------------
# The Sonos speaker leaks into the reSpeaker mic — without muting the
# wake detector during playback we loop: chime plays → mic "hears" →
# fake wake → POST → another chime → repeat. We refresh a 4-s mute
# window every time the player receives a PCM chunk; once the
# orchestrator finishes streaming audio the window expires.
_WAKE_MUTE_AFTER_FEED_S: float = 4.0
# Extra mute applied when the orchestrator signals end-of-stream
# (chime + ack all rendered). Sized for the 1–2 s ringing of a Sonos
# speaker plus the BT codec's lag in returning to silence.
_WAKE_MUTE_AFTER_EOS_S: float = 3.0
_wake_mute_until: float = 0.0
_wake_mute_lock = threading.Lock()
# Moonshine's streaming Transcriber is stateful (start/add/stop over an
# internal VAD stream ID). It is NOT safe to call from multiple
# threads concurrently — the C-API assertion
#   "Adding new audio for stream with ID 1 but VAD is not active"
# fires when two utterances overlap. With single-user semantics we
# serialise: if a turn is already in flight, drop the overlapping
# wake word instead of double-firing the orchestrator.
_handle_lock = threading.Lock()


def _prewarm_ollama() -> None:
    """Force ollama to load the active chat model + keep it resident.

    /api/tags only *lists* models — it does not load them. To collapse
    the cold-load tax on the first wake we POST a 1-token /api/generate
    request with `keep_alive: "30m"` so the weights stay in RAM for the
    configured OLLAMA_KEEP_ALIVE duration. Best-effort — network or
    ollama-not-yet-ready errors are logged but never abort boot.

    Model name resolution: the orchestrator runs ollama with the tag we
    created from `models/voice-assistant.template.Modelfile`
    (`voice-assistant` by default). We push to whichever name
    `RPI_LLM_MODEL` says, falling back to `voice-assistant` so the
    prewarm hits exactly what the streaming chat endpoint will hit.
    """
    if not SAT_PREWARM:
        return
    import urllib.request, json as _json, os
    model_name = (os.environ.get("RPI_LLM_MODEL") or "voice-assistant").strip()
    if not model_name:
        model_name = "voice-assistant"
    # The Ollama /api/generate endpoint lives next to /api/chat on the
    # same host. `tags_url` is e.g. http://ollama:11434/api/tags; we
    # # strip the suffix and tack on /api/generate.
    gen_url = OLLAMA_TAGS_URL.removesuffix("/api/tags").removesuffix("/") + "/api/generate"
    body = {
        "model": model_name,
        "prompt": "hi",
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_predict": 1, "temperature": 0.0},
    }
    req = urllib.request.Request(
        gen_url,
        data=_json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _ = r.read()
        log.info("prewarm ollama ok; model '%s' loaded and resident", model_name)
    except Exception as e:
        log.warning("prewarm ollama failed (cold-load on first wake expected): %s", e)


def _prewarm_piper() -> None:
    """Hit piper /info and a no-op synth (if exposed) so the WAV synth path
    is hot. Same best-effort policy as the ollama pre-warm."""
    if not SAT_PREWARM:
        return
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(PIPER_HEALTH_URL, timeout=4) as r:
            body = _json.loads(r.read().decode("utf-8"))
        engine = body.get("engine", "?")
        voice = body.get("voice", "?")
        log.info("prewarm piper ok; engine=%s voice=%s", engine, voice)
    except Exception as e:
        log.warning("prewarm piper failed (cold-synth on first reply expected): %s", e)


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
                # sounddevice's RawOutputStream callback signature is
                # (outdata: bytes-like, frames: int, time, status) — outdata
                # has no .shape so we drive the loop from `frames`.
                callback=lambda out, frames, _t, _s: self._on_audio(out, frames),
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
        # Extend the wake-word mute window so the reSpeaker mic doesn't
        # interpret its own chime/ack as a fresh wake phrase. We pick a
        # quiet window that grows with the chunk length so a single
        # long Piper sentence still mutes the whole phrase (>2 s) while
        # a stale tail of silence drops the lock.
        ms_of_audio = chunk_int16.size / self._sr * 1000.0
        mute_for_s = max(_WAKE_MUTE_AFTER_FEED_S, ms_of_audio / 1000.0 + 1.5)
        global _wake_mute_until
        with _wake_mute_lock:
            _wake_mute_until = max(_wake_mute_until, time.monotonic() + mute_for_s)
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
        # The orchestrator just finished streaming audio (chime + ack
        # all drained). Keep the wake detector muted for another
        # ~WAKE_MUTE_AFTER_EOS_S so the reSpeaker doesn't catch the
        # trailing edge of the Sonos playback and re-fire the wake
        # word. The chime's last note and the piper sentence's final
        # phoneme can ring in the room for ~1–2 s; the extra mute
        # window absorbs that without making the system feel slow.
        global _wake_mute_until
        with _wake_mute_lock:
            _wake_mute_until = max(
                _wake_mute_until, time.monotonic() + _WAKE_MUTE_AFTER_EOS_S
            )
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _on_audio(self, outdata, frames: int) -> None:
        remaining = frames
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
# Chime cache (process-wide)                                                  #
# --------------------------------------------------------------------------- #
# Rendered lazily on first use; once cached it's a static array reused on
# every tool_call chime. Keeps the SD queue push O(1) regardless of how
# many tools fire per session.
_CHIME_PCM: Optional[np.ndarray] = None


def get_chime_pcm() -> np.ndarray:
    global _CHIME_PCM
    if _CHIME_PCM is None:
        _CHIME_PCM = _synthesise_chime()
        log.info(
            "chime cache built: %.0f samples (%.0f ms @ %d Hz)",
            _CHIME_PCM.size, _CHIME_PCM.size / SPK_SAMPLE_RATE * 1000, SPK_SAMPLE_RATE,
        )
    return _CHIME_PCM


# --------------------------------------------------------------------------- #
# Streaming chat — chunked HTTP from /chat/stream                              #
# --------------------------------------------------------------------------- #
def stream_chat(
    text_in: str,
    transcriber: Transcriber,
    player: ChunkPlayer,
    q: "queue.Queue[Optional[np.ndarray]]",
    tools: Optional[list] = None,
) -> None:
    """POST text to /chat/stream, draining NDJSON into player + transcriber.

    On `{"type":"text_delta",...}` we accumulate incrementally.
    On `{"type":"audio_delta",...}` we decode base64 + strip the WAV
    header (after the very first chunk) and queue the int16 PCM.
    On `{"type":"chime",...}` we queue the rendered arpeggio so the user
      hears something alive during the tool-execution window.
    On `{"type":"done"}` we finalize.
    """
    try:
        url = f"{ASST_BASE_URL}/chat/stream"
        log.info("USER: %s (POST %s)", text_in, url)
        text_buf: list[str] = []
        first_audio = True
        payload: dict = {"text": text_in}
        if tools:
            payload["tools"] = tools
        with requests.post(
            url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
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
                elif kind == "chime":
                    if not SAT_CHIME_ON_TOOL:
                        log.debug("chime suppressed by SAT_CHIME_ON_TOOL=0")
                        continue
                    log.info("CHIME — orchestrator signalled tool ack")
                    player.feed(get_chime_pcm())
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

    # The reSpeaker USB Audio gadget ships its PCM playback controls at
    # ~62% by default and exposes a *separate* mono PCM control (PCM,1)
    # that store-load doesn't raise along with the stereo one. If we
    # leave both at the factory defaults the user gets a barely-audible
    # response even though the playback stream itself opens. Bump both
    # to 100% via amixer so the satellite speaks at the same loudness
    # regardless of host config. Failures here are non-fatal — amixer
    # is not always present (e.g. minimal containers) — so we log warn
    # and continue instead of aborting boot.
    _max_speaker_volume(out_dev)

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

    # ---- boot pre-warm + chime cache ----------------------------------- #
    # Render the arpeggio once so the first tool ack doesn't pay the
    # ~5 ms synth cost. The pre-warm probes are best-effort; failures
    # just mean a cold-load latency on the first wake of a freshly
    # rebooted Pi, not a fatal error.
    try:
        get_chime_pcm()  # populates _CHIME_PCM and logs its size
    except Exception as e:
        log.warning("chime render failed: %s", e)
    _prewarm_ollama()
    _prewarm_piper()

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

                # Drop wake-word events while the playback (chime / TTS
                # ack) is on. Without this the reSpeaker captures the
                # Sonos output, the wake model scores "hey rhasspy" off
                # the chime waveform, and we loop into a wake + chime +
                # wake + chime cascade.
                with _wake_mute_lock:
                    muted = time.monotonic() < _wake_mute_until

                if state == "idle":
                    if muted:
                        if DEBUG:
                            log.debug("wake suppressed (mute window)")
                    else:
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
                        # Snapshot the captured blocks for the worker thread
                        # BEFORE we reset the live buffer; the worker may
                        # still be reading while the main loop already
                        # appended new wake-word frames.
                        captured_chunks: list[np.ndarray] = rec_buffer
                        rec_buffer = []
                        state = "idle"
                        threading.Thread(
                            target=_handle_utterance,
                            args=(captured_chunks, transcriber, out_q, player),
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
    chunks: list[np.ndarray],
    transcriber: Transcriber,
    out_q: "queue.Queue[Optional[np.ndarray]]",
    player: ChunkPlayer,
) -> None:
    """Run ASR + chat for one captured utterance.

    [chunks] is the live list of 80 ms int16 frames captured between
    wake-detection and end-of-utterance. When SAT_STREAMING_ASR is on
    (default), we push those frames into moonshine incrementally so the
    transcript is already settled by the time we're called here; this
    shaves ~300 ms off the end-to-end latency vs the batch
    `transcribe_without_streaming` path. With streaming off, we
    concatenate into one PCM array before feeding the offline transcribe.

    Wraps the body in a non-blocking [_handle_lock] because moonshine's
    streaming Transcriber is process-state — calling start() twice
    without an intervening stop() trips
      "Adding new audio for stream with ID 1 but VAD is not active"
    on the second invocation. With single-user semantics we serialise:
    a wake that fires while the previous turn is still in flight is
    dropped with a log so it can be revisited as barge-in work.
    """
    if not _handle_lock.acquire(blocking=False):
        log.warning("previous turn still in flight — dropping overlapping wake")
        return
    try:
        _handle_utterance_locked(chunks, transcriber, out_q, player)
    finally:
        _handle_lock.release()


def _handle_utterance_locked(
    chunks: list[np.ndarray],
    transcriber: Transcriber,
    out_q: "queue.Queue[Optional[np.ndarray]]",
    player: ChunkPlayer,
) -> None:
    if not chunks:
        log.info("utterance empty — discard")
        return
    captured = np.concatenate(chunks)
    if captured.size < int(SAMPLE_RATE * 0.3):
        log.info("utterance too short (%d samples) — discard", captured.size)
        return

    text_in: str = ""
    if SAT_STREAMING_ASR:
        try:
            text_in = streaming_transcribe(transcriber, chunks)
        except Exception as e:
            log.warning("streaming ASR failed; falling back to offline: %s", e)
            text_in = ""
        if not text_in:
            # Streaming gave nothing back (rare with the medium-streaming
            # model on a very short turn); try the offline path so the
            # user does not silently lose their request.
            log.info("no streaming transcript — re-running offline")
            floats = pcm16_to_float32(captured)
            try:
                text_in = offline_transcribe(transcriber, floats)
            except Exception as e:
                log.warning("offline ASR fallback failed: %s", e)
                return
    else:
        floats = pcm16_to_float32(captured)
        text_in = offline_transcribe(transcriber, floats)

    if not text_in:
        log.info("STT returned empty; will not call orchestrator")
        return
    stream_chat(text_in, transcriber, player, out_q)


def streaming_transcribe(
    transcriber: Transcriber,
    chunks: list[np.ndarray],
    sr: int = SAMPLE_RATE,
) -> str:
    """Drive moonshine's streaming API over [chunks]; return the latest
    transcript text or "" if moonshine never reconciles any lines."""
    transcriber.start()
    last_text = ""
    try:
        for block in chunks:
            floats = pcm16_to_float32(block)
            transcriber.add_audio(floats.tolist(), sr)
            res = transcriber.update_transcription()
            if res and res.lines:
                joined = " ".join(ln.text for ln in res.lines if ln.text).strip()
                if joined:
                    last_text = joined
    finally:
        try:
            transcriber.stop()
        except Exception:
            pass
    return last_text


def offline_transcribe(
    transcriber: Transcriber,
    floats: np.ndarray,
    sr: int = SAMPLE_RATE,
) -> str:
    """One-shot transcription (the original path)."""
    tr = transcriber.transcribe_without_streaming(floats.tolist(), sr)
    if DEBUG:
        log.debug(
            "moonshine transcript dump: lines=%d, raw=%s",
            len(tr.lines),
            [(ln.text, [w.word if w is not None else None for w in (ln.words or [])]) for ln in tr.lines],
        )
    return " ".join(ln.text for ln in tr.lines if ln.text).strip()


if __name__ == "__main__":
    sys.exit(main())
