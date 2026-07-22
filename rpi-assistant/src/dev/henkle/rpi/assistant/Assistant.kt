package dev.henkle.rpi.assistant

import dev.henkle.rpi.assistant.api.HttpApi
import dev.henkle.rpi.assistant.llm.OllamaKoogBridge
import dev.henkle.rpi.assistant.tts.PiperTtsHttp
import kotlinx.coroutines.runBlocking
import org.slf4j.LoggerFactory

/**
 * Application entry point.
 *
 * Bootstraps:
 *   - LLM bridge (Koog + Ollama)
 *   - TTS bridge (HTTP client for piper1-gpl)
 *   - HTTP API server (Ktor/Netty on RPI_ASSISTANT_HTTP_PORT)
 *
 * Logging is left to whatever SLF4J provider Koog pulls in. The Koog
 * dependency used to drag in `tinylog` + `slf4j-simple` alongside Logback
 * (multiple SLF4J bindings); module.yaml now `exclude:`-s those so Logback
 * is the only provider on the classpath. Default Logback output is fine.
 */
fun main() = runBlocking {
    val log = LoggerFactory.getLogger("rpi-assistant")
    val cfg = AssistantConfig.fromEnv()
    log.info("Starting rpi-assistant (HTTP :{}, model='{}', voice='{}')", cfg.httpPort, cfg.ollamaModel, cfg.piperVoice)
    log.info("LLM: {}", cfg.ollamaBaseUrl)
    log.info("TTS: {}", cfg.piperBaseUrl)

    val llm = OllamaKoogBridge(cfg.ollamaBaseUrl, cfg.ollamaModel)
    val tts = PiperTtsHttp(cfg.piperBaseUrl, cfg.piperVoice)

    val api = HttpApi(
        port = cfg.httpPort,
        modelId = cfg.ollamaModel,
        voiceId = cfg.piperVoice,
        llm = llm,
        tts = tts,
        log = log,
    )

    Runtime.getRuntime().addShutdownHook(Thread {
        log.info("Shutdown signal received")
        runCatching { api.stop() }
        runCatching { tts.close() }
        runCatching { llm.close() }
    })
    api.start()
    Thread.currentThread().join()
}
