package dev.henkle.rpi.assistant

/**
 * Process-wide configuration, all values driven by environment variables so
 * docker-compose can override per-deploy. `fromEnv(env)` lets tests pass a
 * hand-rolled map; default reads `System.getenv()`.
 */
data class AssistantConfig(
    val httpPort: Int,
    val ollamaBaseUrl: String,
    val ollamaModel: String,
    val piperBaseUrl: String,
    val piperVoice: String,
) {
    companion object {
        fun fromEnv(env: Map<String, String> = System.getenv()): AssistantConfig = AssistantConfig(
            httpPort     = env["RPI_ASSISTANT_HTTP_PORT"]?.toInt() ?: 6059,
            ollamaBaseUrl= env["RPI_LLM_BASE_URL"]       ?: "http://localhost:11434",
            ollamaModel  = env["RPI_LLM_MODEL"]          ?: "qwen3-nest-mini",
            piperBaseUrl = env["RPI_TTS_BASE_URL"]       ?: "http://localhost:5000",
            piperVoice   = env["RPI_TTS_VOICE"]          ?: "en_US-lessac-medium",
        )
    }
}
