package dev.henkle.rpi.assistant

import dev.henkle.rpi.assistant.llm.OllamaKoogBridge
import dev.henkle.rpi.assistant.proto.HelloRequest
import dev.henkle.rpi.assistant.proto.HelloResponse
import dev.henkle.rpi.assistant.proto.ID
import dev.henkle.rpi.assistant.proto.PingRequest
import dev.henkle.rpi.assistant.proto.PingResponse
import dev.henkle.rpi.assistant.proto.SubscribeVoiceAssistantRequest
import dev.henkle.rpi.assistant.proto.VoiceAssistantAudio
import dev.henkle.rpi.assistant.proto.VoiceAssistantEvent
import dev.henkle.rpi.assistant.proto.VoiceAssistantEventResponse
import dev.henkle.rpi.assistant.proto.VoiceAssistantRequest
import pbandk.ByteArr
import pbandk.decodeFromByteArray
import pbandk.encodeToByteArray
import dev.henkle.rpi.assistant.tts.PiperTtsHttp
import org.slf4j.Logger
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/**
 * ESPHome voice-assistant server sitting between OHF-Voice/linux-voice-assistant
 * (the satellite, which does wake-word + mic capture + speaker playback) and
 * the local Ollama + Piper1-gpl stack (which does intent + voice-out).
 *
 * Each accepted socket runs as its own SatelliteConnection state machine.
 */
class LocalVoiceOrchestrator(
    private val port: Int = 6053,
    private val llm: OllamaKoogBridge,
    private val tts: PiperTtsHttp,
    private val log: Logger,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val activeSatellites = ConcurrentHashMap<String, SatelliteConnection>()
    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null

    fun start() {
        if (!running.compareAndSet(false, true)) {
            log.warn("Orchestrator already started; ignoring duplicate start()")
            return
        }
        serverSocket = ServerSocket(port)
        log.info("Local Voice Core listening on port {}", port)

        thread(name = "LVA-Accept-Loop", isDaemon = true) {
            val server = serverSocket ?: return@thread
            while (running.get()) {
                try {
                    val socket = server.accept()
                    val conn = SatelliteConnection(socket, llm, tts, log)
                    activeSatellites[socket.remoteSocketAddress.toString()] = conn
                    thread(name = "Satellite-Worker-${socket.remoteSocketAddress}", isDaemon = true) {
                        conn.runLoop()
                        activeSatellites.remove(socket.remoteSocketAddress.toString())
                    }
                } catch (e: Exception) {
                    if (running.get()) log.error("Error accepting socket: {}", e.message, e)
                }
            }
        }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        runCatching { serverSocket?.close() }
        activeSatellites.values.forEach { it.close() }
        activeSatellites.clear()
        scope.cancel()
        log.info("Orchestrator stopped")
    }
}

private class SatelliteConnection(
    private val socket: Socket,
    private val llm: OllamaKoogBridge,
    private val tts: PiperTtsHttp,
    private val log: Logger,
) {
    private val input: InputStream = socket.getInputStream()
    private val output: OutputStream = socket.getOutputStream()
    private var satelliteName = "Unknown Device"
    private val connScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    // VA-mode state, advances as the satellite drives the pipeline.
    private var vaSubscribed = false
    private var recordingActive = false
    private var sttBuffer: StringBuilder? = null
    private var inUtterance: Job? = null

    fun runLoop() {
        try {
            startHeartbeat()
            while (!socket.isClosed) {
                val magicByte = input.read().toUByte()
                if (magicByte != 0u.toUByte()) continue
                val payloadLen = readVarint().toInt()
                if (payloadLen <= 0 || payloadLen > Int.MAX_VALUE) continue
                val payload = ByteArray(payloadLen)
                var read = 0
                while (read < payloadLen) {
                    val n = input.read(payload, read, payloadLen - read)
                    if (n == -1) throw EOFException("Stream cut short during packet parsing")
                    read += n
                }
                val typeId = readVarint()
                handleProtoFrame(typeId, payload)
            }
        } catch (e: Exception) {
            log.info("Disconnected from {}: {}", satelliteName, e.message)
        } finally {
            inUtterance?.let { runCatching { it.cancel() } }
            connScope.cancel()
            close()
        }
    }

    private fun handleProtoFrame(typeId: UInt, payload: ByteArray) {
        when (typeId) {
            HelloRequest.ID -> {
                val req = HelloRequest.decodeFromByteArray(payload)
                satelliteName = req.clientInfo.ifBlank { "Unknown Device" }
                log.info("Handshake from '{}' (api {}.{})", satelliteName, req.apiVersionMajor, req.apiVersionMinor)
                val resp = HelloResponse(apiVersionMajor = 1, apiVersionMinor = 9, serverInfo = "Kotlin Engine")
                sendPacket(HelloResponse.ID, resp.encodeToByteArray())
            }
            PingRequest.ID -> sendPacket(PingRequest.ID, PingRequest().encodeToByteArray())
            PingResponse.ID -> { /* heartbeat ack, ignore */ }
            SubscribeVoiceAssistantRequest.ID -> {
                vaSubscribed = true
                log.info("Voice-assistant mode subscribed by {}", satelliteName)
                sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_WAKE_WORD_START, "voice_assistant_ready")
            }
            VoiceAssistantRequest.ID -> {
                val req = VoiceAssistantRequest.decodeFromByteArray(payload)
                if (req.start) {
                    if (!vaSubscribed) {
                        log.warn("VoiceAssistantRequest(start=true) before subscribe; ignoring")
                        return
                    }
                    if (recordingActive) {
                        log.warn("VoiceAssistantRequest(start=true) while still in pipeline; resetting")
                        inUtterance?.let { runCatching { it.cancel() } }
                    }
                    recordingActive = true
                    sttBuffer = StringBuilder()
                    sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_STT_START, "listening")
                    log.info("Utterance start: {}", req.wakeWordPhrase.ifBlank { "<vad>" })
                } else {
                    recordingActive = false
                    sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_STT_END, "stt_end")
                    runUtterance()
                }
            }
            VoiceAssistantAudio.ID -> {
                // Upstream mic PCM; we don't keep a copy because we trust the
                // STT pipeline (LVA + possibly HA) to produce a transcript in
                // a subsequent VoiceAssistantEventResponse with `name="text"`.
            }
            VoiceAssistantEventResponse.ID -> {
                if (!recordingActive) return
                val ev = VoiceAssistantEventResponse.decodeFromByteArray(payload)
                ev.data.firstOrNull { it.name == "text" }?.let { datum ->
                    sttBuffer?.append(datum.value)?.append(' ')
                }
            }
        }
    }

    /**
     * Called when the satellite closes the recording window. Runs LLM + TTS
     * and pushes the synthesized audio back as VoiceAssistantAudio frames.
     */
    private fun runUtterance() {
        inUtterance = connScope.launch {
            val raw = sttBuffer?.toString()?.trim().orEmpty()
            sttBuffer = null
            if (raw.isBlank()) {
                log.info("Empty transcript; skipping LLM/TTS")
                sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_RUN_END, "empty")
                return@launch
            }
            log.info("User said: \"{}\"", raw)
            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_RUN_START, "llm_call")
            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_INTENT_START, "ollama")
            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_INTENT_END, "intent_resolved")

            val reply = StringBuilder()
            try {
                llm.chat(raw) { delta -> reply.append(delta) }
            } catch (e: Exception) {
                log.error("LLM call failed: {}", e.message, e)
                sendVoiceEvent(
                    VoiceAssistantEvent.VOICE_ASSISTANT_ERROR,
                    "llm_error: ${e.javaClass.simpleName}"
                )
                return@launch
            }
            val text = reply.toString().trim()
            log.info("LLM reply: \"{}\"", text)
            if (text.isBlank()) {
                sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_RUN_END, "empty_reply")
                return@launch
            }

            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_TTS_START, "piper")
            sendPacket(
                VoiceAssistantAudio.ID,
                VoiceAssistantAudio(data = ByteArr.empty, end = false).encodeToByteArray(),
            )
            try {
                val pcm = tts.synthesize(text)
                log.info("Piper produced {} PCM bytes for {} chars", pcm.size, text.length)
                // Ship in roughly 100 ms chunks at 22050 Hz / 16-bit mono: 22050 * 0.1 * 2 = 4410 bytes
                val chunkBytes = 4410
                var offset = 0
                while (offset < pcm.size) {
                    val end = (offset + chunkBytes).coerceAtMost(pcm.size)
                    val slice = pcm.copyOfRange(offset, end)
                    sendPacket(
                        VoiceAssistantAudio.ID,
                        VoiceAssistantAudio(
                            data = ByteArr(array = slice),
                            end = false,
                        ).encodeToByteArray(),
                    )
                    offset = end
                }
                sendPacket(
                    VoiceAssistantAudio.ID,
                    VoiceAssistantAudio(data = ByteArr.empty, end = true).encodeToByteArray(),
                )
            } catch (e: Exception) {
                log.error("Piper synthesis failed: {}", e.message, e)
                sendVoiceEvent(
                    VoiceAssistantEvent.VOICE_ASSISTANT_ERROR,
                    "tts_error: ${e.javaClass.simpleName}"
                )
            }
            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_TTS_END, "done")
            sendVoiceEvent(VoiceAssistantEvent.VOICE_ASSISTANT_RUN_END, "ok")
        }
    }

    private fun sendVoiceEvent(type: VoiceAssistantEvent, name: String) {
        val ev = VoiceAssistantEventResponse(
            eventType = type,
            data = listOf(dev.henkle.rpi.assistant.proto.VoiceAssistantEventData(name = name, value = "")),
        )
        sendPacket(VoiceAssistantEventResponse.ID, ev.encodeToByteArray())
    }

    fun sendPacket(typeId: UInt, payload: ByteArray) {
        synchronized(output) {
            if (socket.isClosed) return
            output.write(0x00) // magic plaintext byte
            writeVarint(payload.size.toUInt())
            writeVarint(typeId)
            output.write(payload)
            output.flush()
        }
    }

    private fun startHeartbeat() {
        thread(name = "Heartbeat-$satelliteName", isDaemon = true) {
            while (!socket.isClosed) {
                try {
                    Thread.sleep(25_000)
                    sendPacket(PingRequest.ID, PingRequest().encodeToByteArray())
                } catch (_: Exception) {
                    break
                }
            }
        }
    }

    private fun readVarint(): UInt {
        var result = 0u
        var shift = 0
        while (true) {
            val raw = input.read()
            if (raw == -1) throw EOFException("Unexpected end of stream parsing varint")
            val b = raw.toUByte()
            result = result or ((b.toUInt() and 0x7Fu) shl shift)
            if ((raw and 0x80) == 0) break
            shift += 7
            if (shift >= 35) throw IllegalArgumentException("Varint stream overflow")
        }
        return result
    }

    private fun writeVarint(value: UInt) {
        var v = value
        while (v > 0x7Fu) {
            output.write((v and 0x7Fu).toInt() or 0x80)
            v = v shr 7
        }
        output.write((v and 0x7Fu).toInt())
    }

    fun close() {
        runCatching { socket.close() }
        log.info("Connection with {} closed.", satelliteName)
    }
}
