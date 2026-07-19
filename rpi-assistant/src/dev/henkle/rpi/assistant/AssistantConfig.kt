package dev.henkle.rpi.assistant

data class AssistantConfig(
    val orchestratorPort: Int,
    val ollamaBaseUrl: String,
    val ollamaModel: String,
    val piperBaseUrl: String,
    val piperVoice: String,
) {
    companion object {
        fun fromEnv(env: Map<String, String> = System.getenv()): AssistantConfig = AssistantConfig(
            orchestratorPort = env["RPI_ORCHESTRATOR_PORT"]?.toInt() ?: 6053,
            ollamaBaseUrl    = env["RPI_LLM_BASE_URL"]       ?: "http://ollama:11434",
            ollamaModel      = env["RPI_LLM_MODEL"]          ?: "qwen3-nest-mini",
            piperBaseUrl     = env["RPI_TTS_BASE_URL"]       ?: "http://piper:5000",
            piperVoice       = env["RPI_TTS_VOICE"]          ?: "en_US-lessac-medium",
        )
    }
}
