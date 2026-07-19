package dev.henkle.rpi.assistant.llm

import ai.koog.prompt.dsl.prompt
import ai.koog.prompt.executor.clients.LLMClient
import ai.koog.prompt.executor.ollama.client.OllamaClient
import ai.koog.prompt.llm.LLMCapability
import ai.koog.prompt.llm.LLMProvider
import ai.koog.prompt.llm.LLModel
import ai.koog.prompt.streaming.StreamFrame

/**
 * Bridges the ESPHome voice pipeline to a local Ollama-served LLM via Koog.
 * Streams the agent's reply token-by-token so the orchestrator can drive
 * Piper asynchronously alongside Ollama's response.
 */
class OllamaKoogBridge(baseUrl: String, modelId: String) : AutoCloseable {
    private val client: LLMClient = OllamaClient(baseUrl = baseUrl)
    private val llmModel = LLModel(
        provider = LLMProvider.Ollama,
        id = modelId,
        capabilities = listOf(LLMCapability.Completion, LLMCapability.ToolChoice),
    )

    /**
     * Streams [userText] through the LLM, invoking [onDelta] for every text
     * chunk as it arrives. Returns the full accumulated reply.
     */
    suspend fun chat(userText: String, onDelta: suspend (String) -> Unit): String {
        val prompt = prompt("voice-assistant-${System.nanoTime()}") {
            system(
                "You are a privacy-first voice assistant running on a Raspberry Pi. " +
                    "Reply in 1-3 short, conversational sentences. " +
                    "No markdown, no lists, no code blocks. If you do not know the answer, " +
                    "say so plainly; do not invent."
            )
            user(userText)
        }
        val responseText = StringBuilder()
        client.executeStreaming(prompt = prompt, model = llmModel).collect { frame ->
            when (frame) {
                is StreamFrame.TextDelta -> {
                    responseText.append(frame.text)
                    onDelta(frame.text)
                }
                is StreamFrame.End -> Unit
                else -> Unit
            }
        }
        return responseText.toString()
    }

    override fun close() = client.close()
}
