package dev.henkle.rpi.assistant.tts

import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsBytes
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.Closeable

/**
 * Client for the piper1-gpl HTTP server (`POST /synthesize`). Returns the raw
 * WAV payload Piper emits — RIFF header + 16-bit little-endian PCM; the
 * satellite (Python sounddevice) decodes the WAV. Previous behaviour of
 * stripping the 44-byte RIFF header was for the old ESPHome VoiceAssistant
 * wire format and is no longer needed.
 */
class PiperTtsHttp(private val baseUrl: String, private val voice: String) : Closeable {
    private val client = HttpClient(CIO) {
        install(ContentNegotiation) { json(Json { ignoreUnknownKeys = true }) }
        engine { requestTimeout = 60_000 }
    }

    suspend fun synthesize(text: String): ByteArray = withContext(Dispatchers.IO) {
        val response = client.post("$baseUrl/synthesize") {
            contentType(ContentType.Application.Json)
            setBody(buildJsonObject {
                put("text", text)
                put("voice", voice)
            })
        }
        response.bodyAsBytes()
    }

    override fun close() = client.close()
}
