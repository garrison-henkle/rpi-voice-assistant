"""Mock LLM + Piper HTTP servers for end-to-end Mac testing of /chat/stream.

Responses are deterministic. Piper endpoint returns a fixed 22050 Hz int16
PCM sine wave so we can verify the satellite chunks are realistic without
running real speech synthesis.
"""
import http.server
import json
import math
import os
import socketserver
import struct
import sys
import threading
import time

WAVE_HEADER = (
    b"RIFF" + struct.pack("<I", 36 + 36)  # file size placeholder
    + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 22050, 44100, 2, 16)
    + b"data"  + struct.pack("<I", 36)  # data size placeholder
)


def fake_wav_pcm(text: str) -> bytes:
    """Build a 22050 Hz int16 mono WAV using the length of `text` as duration."""
    sr = 22050
    n = int(sr * (0.06 * len(text) + 0.4))
    pcm = bytearray()
    for i in range(n):
        v = int(0.2 * 32767 * math.sin(2 * math.pi * 220 * i / sr))
        pcm += struct.pack("<h", v)
    body = bytes(pcm)
    data_size = len(body)
    riff_size = 36 + data_size
    return (
        b"RIFF" + struct.pack("<I", riff_size)
        + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
        + b"data" + struct.pack("<I", data_size)
        + body
    )


class OllamaMockServer(http.server.BaseHTTPRequestHandler):
    """Speaks enough of /api/chat (streaming NDJSON) to satisfy Koog's OllamaClient."""

    def log_message(self, *args, **kwargs):
        sys.stderr.write(f"[ollama-mock] {self.path}\n")

    def do_POST(self):  # noqa
        if self.path in ("/api/chat", "/v1/chat/completions", "/api/generate"):
            length = int(self.headers.get("Content-Length", "0"))
            body_raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(body_raw.decode("utf-8")) if body_raw else {}
            except Exception:
                body = {}
            user_text = ""
            msgs = body.get("messages") or []
            for m in msgs:
                if m.get("role") == "user":
                    user_text = m.get("content", "")
            if not user_text:
                prompt = body.get("prompt", "")
                user_text = prompt if isinstance(prompt, str) else ""
            prompt = body.get("prompt", "")
            if isinstance(prompt, str) and not user_text:
                user_text = prompt
            sentence = f"You said: '{user_text}'. I am a mock LLM."
            tokens = sentence.split(" ")

            # Build the entire NDJSON reply up front so we can advertise a
            # real Content-Length; sending live streaming + Transfer-Encoding
            # chunked under BaseHTTP corrupts the wire and Koog's
            # KtorKoogHttpClient rejects it with "Bad chunk header". Real
            # Ollama has its own framing Koog speaks natively (it pulls each
            # NDJSON record out of a single readable stream).
            #
            # NOTE: Koog's OllamaClient parses each line into
            # OllamaChatResponseDTO(model: String, message?, done: Boolean, ...).
            # `model` is REQUIRED (no default), so we must include a value
            # on every line, otherwise the kotlinx-serialization decoder
            # silently drops the line and no TextDelta is ever emitted.
            payload_lines: list[bytes] = []
            MODEL = "mock-llm"
            for w in tokens:
                if self.path == "/api/chat":
                    payload_lines.append(
                        (json.dumps({
                            "model": MODEL,
                            "message": {"role": "assistant", "content": w + " "},
                            "done": False,
                        }) + "\n").encode()
                    )
                else:
                    payload_lines.append(
                        (json.dumps({"model": MODEL, "response": w + " ", "done": False}) + "\n").encode()
                    )
            if self.path == "/api/chat":
                payload_lines.append(
                    (json.dumps({
                        "model": MODEL,
                        "message": {"role": "assistant", "content": ""},
                        "done": True,
                        "total_duration": 0,
                        "prompt_eval_count": 0,
                        "eval_count": len(tokens),
                    }) + "\n").encode()
                )
            else:
                payload_lines.append(
                    (json.dumps({"model": MODEL, "response": "", "done": True, "total_duration": 0, "prompt_eval_count": 0, "eval_count": len(tokens)}) + "\n").encode()
                )

            # Pad with a "stream" delay by yawning real time between lines
            # is impossible with a single Content-Length, so we just send
            # the whole thing at once. Prosody / cadence can wait; this
            # exercises the protocol shape correctly.
            body = b"".join(payload_lines)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class PiperMockServer(http.server.BaseHTTPRequestHandler):
    """Speaks enough of /synthesize to satisfy our Piper HTTP client."""

    def log_message(self, *args, **kwargs):
        sys.stderr.write(f"[piper-mock] {self.path}\n")

    def do_POST(self):  # noqa
        if self.path == "/synthesize":
            length = int(self.headers.get("Content-Length", "0"))
            body_raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(body_raw.decode("utf-8")) if body_raw else {}
            except Exception:
                body = {}
            text = body.get("text", "")
            wav = fake_wav_pcm(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
            return
        if self.path == "/info":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"voice":"en_US-hfc_male-medium"}')
            return
        self.send_error(404)


def serve_http(handler_cls, port: int):
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), handler_cls) as srv:
        print(f"serving {handler_cls.__name__} on :{port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    ports = json.loads(os.environ.get("MOCK_PORTS", '{"ollama":11434,"piper":5050}'))
    t_oll = threading.Thread(target=serve_http, args=(OllamaMockServer, ports["ollama"]), daemon=True)
    t_pipe = threading.Thread(target=serve_http, args=(PiperMockServer, ports["piper"]), daemon=True)
    t_oll.start()
    t_pipe.start()
    print("mocks running; ctrl-c to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)
