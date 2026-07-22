package dev.henkle.rpi.assistant.api

import dev.henkle.rpi.assistant.llm.OllamaKoogBridge
import dev.henkle.rpi.assistant.tts.PiperTtsHttp
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.install
import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receiveText
import io.ktor.server.response.respondText
import io.ktor.server.response.respondTextWriter
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.routing
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.slf4j.Logger
import java.util.Base64

/**
 * Thin REST surface spoken by the satellite. Three endpoints:
 *   GET  /info          – capability advertisement
 *   POST /chat          – text in, text + base64-WAV audio out (one-shot)
 *   POST /chat/stream   – text in, NDJSON out: per-token text_delta +
 *                         per-sentence audio_delta + done (chunked)
 *
 * Wire:
 *   chat request:    {"text": "...", "voice"?: "..."}
 *   chat response:   {"text": "...", "audio_b64": "...", "audio_format": "wav"}
 *   info response:   {"name": "rpi-assistant", "model": "...", "voice": "...", "ok": true}
 *   stream response: NDJSON, one event per line:
 *                     {"type":"text_delta","text":"..."}
 *                     {"type":"audio_delta","data":"<base64-wav>"}
 *                     {"type":"done"}
 *
 * Streaming chunks the LLM reply at sentence boundaries (`.`, `!`, `?`)
 * OR after a 40-char run-on threshold and synthesises each slice on Piper
 * while the LLM continues producing. The first audio_delta includes its
 * 44-byte RIFF header so the satellite can feed it to a playback device;
 * subsequent chunks ship header-stripped PCM that the satellite stitches
 * after the first.
 *
 * We avoid the kotlinx-serialization compiler plugin on purpose: rpi-assistant
 * only needs to encode/decode JSON with a handful of fields, so the runtime
 * [JsonObject] / [buildJsonObject] API is enough.
 */
class HttpApi(
    private val port: Int,
    private val modelId: String,
    private val voiceId: String,
    private val llm: OllamaKoogBridge,
    private val tts: PiperTtsHttp,
    private val log: Logger,
) {
    private val server = embeddedServer(Netty, port = port) {
        configureModule()
    }

    fun start() {
        server.start(wait = false)
    }

    fun stop() {
        server.stop(gracePeriodMillis = 1000, timeoutMillis = 5_000)
    }

    private fun Application.configureModule() {
        install(StatusPages) {
            exception<Throwable> { call, cause ->
                log.error("Unhandled error on {} {}", call.request.local.method, call.request.local.uri, cause)
                call.respondText(
                    text = buildJsonObject {
                        put("text", "(internal error: ${cause::class.simpleName ?: "unknown"})")
                        put("audio_b64", "")
                        put("audio_format", "wav")
                    }.toString(),
                    contentType = ContentType.Application.Json,
                    status = HttpStatusCode.InternalServerError,
                )
            }
        }
        routing {
            get("/info") {
                val body = buildJsonObject {
                    put("name", "rpi-assistant")
                    put("model", modelId)
                    put("voice", voiceId)
                    put("ok", true)
                }
                call.respondText(body.toString(), ContentType.Application.Json)
            }

            post("/chat") {
                val rawBody = call.receiveText()
                log.info("/chat request body: {}", rawBody.take(200))
                val parsed: JsonObject = try {
                    Json.parseToJsonElement(rawBody).let { it as? JsonObject }
                        ?: run {
                            call.respondText(
                                text = chatErrorBody("body must be a JSON object"),
                                contentType = ContentType.Application.Json,
                                status = HttpStatusCode.BadRequest,
                            )
                            return@post
                        }
                } catch (e: Exception) {
                    call.respondText(
                        text = chatErrorBody("invalid JSON: ${e.message ?: "?"}"),
                        contentType = ContentType.Application.Json,
                        status = HttpStatusCode.BadRequest,
                    )
                    return@post
                }
                val textIn = parsed["text"]?.toString()?.trim('"') ?: ""
                if (textIn.isBlank()) {
                    call.respondText(
                        text = chatErrorBody("missing 'text'"),
                        contentType = ContentType.Application.Json,
                        status = HttpStatusCode.BadRequest,
                    )
                    return@post
                }
                log.info("/chat text='{}'", textIn)
                val responseText = llm.chat(textIn) { /* drop streaming deltas */ }
                log.info("/chat answer='{}'", responseText)
                val wav = withContext(Dispatchers.IO) { tts.synthesize(responseText) }
                val body = buildJsonObject {
                    put("text", responseText)
                    put("audio_b64", Base64.getEncoder().encodeToString(wav))
                    put("audio_format", "wav")
                }
                call.respondText(body.toString(), ContentType.Application.Json)
            }

            post("/chat/stream") {
                val rawBody = call.receiveText()
                log.info("/chat/stream request body: {}", rawBody.take(200))
                val parsed: JsonObject = try {
                    Json.parseToJsonElement(rawBody).let { it as? JsonObject }
                        ?: throw IllegalArgumentException("body must be a JSON object")
                } catch (e: Exception) {
                    call.respondText(
                        text = chatErrorBody("invalid JSON: ${e.message ?: "?"}"),
                        contentType = ContentType.Application.Json,
                        status = HttpStatusCode.BadRequest,
                    )
                    return@post
                }
                val textIn = parsed["text"]?.toString()?.trim('"') ?: ""
                if (textIn.isBlank()) {
                    call.respondText(
                        text = chatErrorBody("missing 'text'"),
                        contentType = ContentType.Application.Json,
                        status = HttpStatusCode.BadRequest,
                    )
                    return@post
                }
                log.info("/chat/stream text='{}'", textIn)

                val firstAudio = booleanArrayOf(true)
                val unfinished = StringBuilder()

                call.respondTextWriter(contentType = ContentType("application", "x-ndjson")) {
                    fun emit(event: JsonObject) {
                        write(event.toString())
                        write("\n")
                        flush()
                    }

                    suspend fun flushSlice(slice: String, isFinal: Boolean): Boolean {
                        if (!isFinal && slice.isBlank()) return true
                        val wav = withContext(Dispatchers.IO) { tts.synthesize(slice) }
                        val b64 = if (firstAudio[0]) {
                            firstAudio[0] = false
                            Base64.getEncoder().encodeToString(wav)
                        } else {
                            // Strip the 44-byte RIFF header on every subsequent chunk
                            // so the satellite can stitch raw 22 kHz int16 PCM back
                            // to back without re-parsing.
                            val body = if (wav.size >= 44 && wav.copyOfRange(0, 4).contentEquals("RIFF".toByteArray()))
                                wav.copyOfRange(44, wav.size) else wav
                            Base64.getEncoder().encodeToString(body)
                        }
                        emit(buildJsonObject {
                            put("type", "audio_delta")
                            put("data", b64)
                        })
                        return true
                    }

                    val response = llm.chat(textIn) { delta ->
                        emit(buildJsonObject {
                            put("type", "text_delta")
                            put("text", delta)
                        })
                        unfinished.append(delta)
                        val pending = unfinished.toString()
                        val boundary = lastSentenceBoundary(pending)
                        if (boundary > 0) {
                            val slice = pending.substring(0, boundary + 1).trim()
                            val remainder = pending.substring(boundary + 1)
                            unfinished.clear()
                            unfinished.append(remainder)
                            if (slice.isNotBlank() && slice.length >= MIN_FLUSH_CHARS) {
                                flushSlice(slice, isFinal = false)
                            }
                        } else if (pending.length >= MAX_FLUSH_CHARS) {
                            flushSlice(pending.trim(), isFinal = false)
                            unfinished.clear()
                        }
                    }
                    log.info("/chat/stream answer='{}'", response)
                    if (unfinished.isNotEmpty()) {
                        flushSlice(unfinished.toString().trim(), isFinal = true)
                        unfinished.clear()
                    }
                    emit(buildJsonObject { put("type", "done") })
                }
            }
        }
    }

    private fun chatErrorBody(message: String): String = buildJsonObject {
        put("text", message)
        put("audio_b64", "")
        put("audio_format", "wav")
    }.toString()

    private fun lastSentenceBoundary(text: String): Int {
        // Scan from the end for the last `.`, `!`, or `?` character. Returns -1 if
        // there are no sentence terminators yet.
        for (i in text.length - 1 downTo 0) {
            val c = text[i]
            if (c == '.' || c == '!' || c == '?') return i
        }
        return -1
    }

    companion object {
        private const val MIN_FLUSH_CHARS = 24
        private const val MAX_FLUSH_CHARS = 90
    }
}
