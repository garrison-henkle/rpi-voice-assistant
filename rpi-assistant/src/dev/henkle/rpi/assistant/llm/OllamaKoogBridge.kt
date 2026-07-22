package dev.henkle.rpi.assistant.llm

import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.utils.io.readUTF8Line
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * Streams a chat reply from a local Ollama-served LLM. Implements just the
 * pieces we need from ollama's /api/chat wire format and bypasses Koog's
 * `OllamaClient` because Koog never passes `think:false` and qwen3 defaults
 * to thinking mode (which silently fills the `thinking` field while leaving
 * `content` empty). It also strips `<think>...</think>` trailers in case
 * ollama falls back to inline reasoning on a long prompt.
 *
 * The streaming contract here is the same that the rest of the app expects:
 * every text token we receive invokes [onDelta] immediately so the
 * orchestrator can run Piper alongside the model output.
 */
class OllamaKoogBridge(baseUrl: String, modelId: String) : AutoCloseable {
    private val client = HttpClient(CIO) {
        install(HttpTimeout) {
            requestTimeoutMillis = 120_000
            connectTimeoutMillis = 5_000
            socketTimeoutMillis = 120_000
        }
    }
    private val modelId = modelId
    private val chatEndpoint = "$baseUrl/api/chat"
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    suspend fun chat(userText: String, onDelta: suspend (String) -> Unit): String {
        val body = buildJsonObject {
            put("model", modelId)
            put("stream", true)
            // qwen3 in ollama defaults to thinking mode; force it off so
            // `message.content` actually carries the visible reply rather
            // than the chain-of-thought.
            put("think", false)
            put(
                "messages",
                JsonArray(
                    listOf(
                        buildJsonObject {
                            put("role", "system")
                            put(
                                "content",
                                "You are a voice assistant named Rhasspy. " +
                                    "Reply in 1-3 short, conversational sentences. " +
                                    "No markdown, no lists, no code blocks. If you do not " +
                                    "know the answer, say so plainly; do not invent."
                            )
                        },
                        buildJsonObject { put("role", "user"); put("content", userText) },
                    )
                )
            )
        }

        val responseText = StringBuilder()
        withContext(Dispatchers.IO) {
            val response = client.post(chatEndpoint) {
                contentType(ContentType.Application.Json)
                setBody(body.toString())
            }
            require(response.status.value in 200..299) {
                "ollama /api/chat returned HTTP ${response.status.value} for model '$modelId'"
            }
            val channel = response.bodyAsChannel()
            val pendingBuffer = StringBuilder()
            while (!channel.isClosedForRead) {
                val line = channel.readUTF8Line() ?: break
                if (line.isBlank()) continue
                val chunk: JsonObject = try {
                    json.parseToJsonElement(line).jsonObject
                } catch (e: Exception) {
                    continue
                }
                // For thinking-mode fallback: ollama emits the model output in
                // `message.content` AND the model's own reasoning in
                // `message.thinking`. With think=false above, both stay
                // empty for thinking; only `message.content` carries tokens.
                val msg = chunk["message"]?.jsonObject ?: continue
                val content = msg["content"]?.takeIf {
                    it !is kotlinx.serialization.json.JsonNull
                }?.jsonPrimitive?.content ?: continue

                // Belt-and-braces: some configs of qwen3 hybrid-mode still
                // emit `<think>...</think>` inside `content`. Strip those
                // even though they shouldn't be there when think=false.
                if (content.contains("<think>")) {
                    pendingBuffer.append(stripThinking(content))
                } else {
                    pendingBuffer.append(content)
                }
                if (pendingBuffer.isNotEmpty()) {
                    val toEmit = pendingBuffer.toString()
                    pendingBuffer.clear()
                    responseText.append(toEmit)
                    onDelta(toEmit)
                }
            }
        }
        return responseText.toString()
    }

    override fun close() = client.close()

    private fun stripThinking(content: String): String {
        val startIdx = content.indexOf("<think>")
        val endIdx = content.indexOf("</think>")
        if (startIdx == -1 || endIdx == -1 || endIdx < startIdx) return content
        val before = content.substring(0, startIdx)
        val after = content.substring(endIdx + "</think>".length)
        return (before + after).trimStart()
    }
}
