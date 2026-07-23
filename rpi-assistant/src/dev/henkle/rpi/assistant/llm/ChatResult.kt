package dev.henkle.rpi.assistant.llm

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * A single tool invocation the LLM requested via ollama's `tools` field.
 * [name] is the tool/function name; [arguments] is the raw JSON object the
 * model produced (tool-specific schema, validated by the executor).
 */
data class ToolCall(val name: String, val arguments: JsonObject)

/**
 * Outcome of one `OllamaKoogBridge.chat` round-trip.
 *
 * [text] is the visible `message.content` the satellite speaks aloud.
 *   May be empty if the model went straight into tool-calling without
 *   any preamble; that is normal and is what the chime is for.
 * [toolCalls] is the list of tool invocations the model asked for. Empty
 *   for plain conversation; non-empty means the orchestrator must
 *   execute them (and emit a chime so the user hears something during
 *   the gap).
 * [done] is true if the model indicated it is finished producing more
 *   output — never false mid-stream because ollama NDJSON keeps emitting
 *   chunks until `done:true`.
 */
data class ChatResult(
    val text: String,
    val toolCalls: List<ToolCall>,
    val raw: JsonElement? = null,
)
