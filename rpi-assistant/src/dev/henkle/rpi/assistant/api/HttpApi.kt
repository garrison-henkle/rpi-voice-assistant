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
 * Thin REST surface spoken by the satellite. Two endpoints:
 *   GET  /info    – capability advertisement
 *   POST /chat    – text in, text + base64-WAV audio out
 *
 * Wire is plain JSON over HTTP:
 *   chat request:    {"text": "...", "voice"?: "..."}
 *   chat response:   {"text": "...", "audio_b64": "...",
 *                     "audio_format": "wav"}
 *   info response:   {"name": "rpi-assistant",
 *                     "model": "...", "voice": "...", "ok": true}
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
        }
    }

    private fun chatErrorBody(message: String): String = buildJsonObject {
        put("text", message)
        put("audio_b64", "")
        put("audio_format", "wav")
    }.toString()
}
