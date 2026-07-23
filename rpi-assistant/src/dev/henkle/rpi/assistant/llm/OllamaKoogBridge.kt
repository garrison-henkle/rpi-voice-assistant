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
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * Streams a chat reply from a local Ollama-served LLM. Bypasses Koog's
 * `OllamaClient` because Koog never passes `think:false` and qwen3 defaults
 * to thinking mode (which silently fills the `thinking` field while leaving
 * `content` empty). It also strips `<think>...</think>` trailers in case
 * ollama falls back to inline reasoning on a long prompt.
 *
 * Model-agnostic: `think:false` and the `<think>` strip are a no-op for
 * models that don't support either (llama3.2, mistral, phi, …). When
 * [chat] is called with [tools], ollama's response may include
 * `message.tool_calls` JSON; we surface those so the orchestrator can
 * route them to a `ToolExecutor` and decide whether to speak the result
 * or chain a follow-up LLM call.
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
    private val json = kotlinx.serialization.json.Json {
        ignoreUnknownKeys = true
        isLenient = true
    }
    private val systemPrompt = "You are a voice assistant named Rhasspy. " +
        "Reply in 1-3 short, conversational sentences. " +
        "No markdown, no lists, no code blocks. If you do not " +
        "know the answer, say so plainly; do not invent."

    /**
     * Stream a reply from the configured ollama model.
     *
     * @param userText the user's spoken prompt (already transcribed by
     *                 moonshine on the satellite side).
     * @param tools    optional OpenAI-shaped tool schemas; when present
     *                 the model gets a `tools` array and ollama returns
     *                 `message.tool_calls` instead of plain text. Leave
     *                 null for plain conversation.
     * @param onDelta  invoked for every visible text fragment the model
     *                 emits; the satellite-side orchestrator uses this
     *                 to flush Piper slices as they appear. Tool-call
     *                 JSON is **not** emitted here — only `content`.
     * @return [ChatResult] with the final text + collected tool calls.
     */
    /**
     * Stream a reply from the configured ollama model.
     *
     * @param userText the user's spoken prompt (already transcribed by
     *                 moonshine on the satellite side).
     * @param tools    optional OpenAI-shaped tool schemas; when present
     *                 the model gets a `tools` array and ollama returns
     *                 `message.tool_calls` instead of plain text. Leave
     *                 null for plain conversation.
     * @param onDelta  invoked for every visible text fragment the model
     *                 emits; the satellite-side orchestrator uses this
     *                 to flush Piper slices as they appear. Tool-call
     *                 JSON is **not** emitted here — only `content`.
     * @param onToolCall invoked once per unique tool invocation as
     *                 soon as it first appears in the stream. Dedup
     *                 uses `(name, normalized-args)` so even when
     *                 ollama re-emits the same call with progressively
     *                 richer arguments across many chunks, this fires
     *                 exactly once with the final args. Use together
     *                 with [onDelta] to stop forwarding preamble text
     *                 through Piper the moment a tool fires.
     * @return [ChatResult] with the final text + collected tool calls.
     */
    suspend fun chat(
        userText: String,
        tools: List<JsonObject>? = null,
        onDelta: suspend (String) -> Unit,
        onToolCall: suspend (ToolCall) -> Unit = {},
    ): ChatResult {
        val body = buildJsonObject {
            put("model", modelId)
            put("stream", true)
            // qwen3 in ollama defaults to thinking mode; force it off so
            // `message.content` actually carries the visible reply rather
            // than the chain-of-thought. No-op for non-thinking models.
            put("think", false)
            if (!tools.isNullOrEmpty()) {
                put("tools", JsonArray(tools))
            }
            put(
                "messages",
                JsonArray(
                    listOf(
                        buildJsonObject {
                            put("role", "system")
                            put("content", systemPrompt)
                        },
                        buildJsonObject {
                            put("role", "user")
                            put("content", userText)
                        },
                    )
                ),
            )
        }

        val responseText = StringBuilder()
        val responseToolCalls: MutableList<ToolCall> = mutableListOf()
        // Track tool_calls by `(name, normalized-args-signature)` so that
        // — even if ollama re-emits the same tool across multiple chunks
        // (it does this for partial-argument streaming) — we only keep
        // the latest version of each unique invocation. The signature
        // key changes as the argument JSON populates, so first-seen wins
        // the spot and the orchestrator fires `onToolCall` exactly once
        // per distinct tool, with the final (most-populated) arguments.
        val seenToolCalls: MutableMap<String, ToolCall> = LinkedHashMap()
        var lastChunk: JsonElement? = null
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
                lastChunk = chunk
                val msg = chunk["message"]?.jsonObject ?: continue

                // 1. visible content → emit onDelta (always stream tokens as
                // they appear; callers decide whether to flush to Piper).
                val content = msg["content"]?.takeIf {
                    it !is JsonNull
                }?.jsonPrimitive?.content
                if (content != null) {
                    val emitted = if (content.contains("<think>")) stripThinking(content) else content
                    if (emitted.isNotEmpty()) {
                        pendingBuffer.append(emitted)
                        val toEmit = pendingBuffer.toString()
                        pendingBuffer.clear()
                        responseText.append(toEmit)
                        onDelta(toEmit)
                    }
                }

                // 2. tool_calls → dedup by tool *name* (NOT by args) and fire
                // onToolCall on first appearance of each unique name.
                // Ollama streams a single named tool across many chunks
                // where the argument JSON string grows progressively.
                // Deduplicating by (name, args) would fire onToolCall
                // once per chunk as args populate. We instead keep the
                // latest args for each named tool, and we fire
                // onToolCall exactly once per name so the orchestrator
                // can abort piping preamble through Piper — otherwise
                // the user hears "the lights are on" (the model's
                // preamble) and then "turning on the kitchen lights"
                // (the canned ack) back to back.
                collectToolCalls(msg)?.forEach { call ->
                    if (seenToolCalls.containsKey(call.name)) {
                        // Same tool, richer arguments — overwrite the
                        // payload so the orchestrator sees the final
                        // args when it dispatches after done.
                        seenToolCalls[call.name] = call
                    } else {
                        seenToolCalls[call.name] = call
                        onToolCall(call)
                    }
                }

            }
        }
        responseToolCalls.addAll(seenToolCalls.values)
        return ChatResult(
            text = responseText.toString(),
            toolCalls = responseToolCalls.toList(),
            raw = lastChunk,
        )
    }

    /**
     * Extract a flat list of `ToolCall` from a single ollama stream
     * chunk's `message.tool_calls`. Ollama emits the schema as
     * `[{function:{name, arguments}}, ...]`; we accept both `arguments`
     * as a JSON object (typical) and as a stringified JSON (some
     * adapters), parsing the latter defensively.
     */
    private fun collectToolCalls(msg: JsonObject): List<ToolCall>? {
        val calls = msg["tool_calls"] ?: return null
        val arr = calls as? JsonArray ?: return null
        if (arr.isEmpty()) return null
        return arr.mapNotNull { el ->
            val obj = el as? JsonObject ?: return@mapNotNull null
            val fn = obj["function"]?.jsonObject ?: return@mapNotNull null
            val name = fn["name"]?.jsonPrimitive?.contentOrNullSafe() ?: return@mapNotNull null
            val rawArgs = fn["arguments"] ?: return@mapNotNull null
            val argsObj: JsonObject = when (rawArgs) {
                is JsonObject -> rawArgs
                is JsonPrimitive ->
                    parseJsonObjString(rawArgs.content) ?: JsonObject(emptyMap())
                else -> JsonObject(emptyMap())
            }
            ToolCall(name = name, arguments = argsObj)
        }
    }

    private fun parseJsonObjString(s: String): JsonObject? = try {
        json.parseToJsonElement(s).jsonObject
    } catch (e: Exception) {
        null
    }

    private fun JsonElement.contentOrNullSafe(): String? =
        if (this is JsonPrimitive) content else null

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
