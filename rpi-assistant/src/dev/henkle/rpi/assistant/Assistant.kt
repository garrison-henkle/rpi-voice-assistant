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
 * Note on logging: Koog's transitive graph pulls in
 * `tinylog-api` + `slf4j-simple` alongside `logback-classic`. SLF4J prints
 * the "multiple bindings" warning at startup and picks whichever provider
 * is first on the classpath. Tolerant of any provider: we do whatever we
 * can via SLF4J's standard API (none, in practice) and fall back to
 * system-property hints per provider.
 */
fun main() = runBlocking {
    val log = LoggerFactory.getLogger("rpi-assistant")
    configureLogging(log)

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

/**
 * Detect which SLF4J provider is active and apply best-effort configuration
 * knobs via system properties (the only SLF4J-standard mechanism that works
 * across providers). We **do not** cast to Logback's LoggerContext — if the
 * active provider isn't Logback, we just leave its defaults alone.
 */
private fun configureLogging(log: org.slf4j.Logger) {
    val factoryClass = runCatching { LoggerFactory.getILoggerFactory().javaClass.name }
        .getOrDefault("(unavailable)")
    log.info("SLF4J provider: {}", factoryClass)

    when {
        factoryClass.contains("Tinylog") -> {
            // Tinylog reads "tinylog.*" + "tinylog.properties"-file at init;
            // set the most useful ones inline.
            System.setProperty("tinylog.format", "{date: HH:mm:ss.SSS} {level} [{thread}] {class}.{method} - {message}")
            System.setProperty("tinylog.level", "INFO")
        }
        factoryClass.contains("ch.qos.logback") -> {
            // Logback: silence the noisier subsystems. Configuration via
            // logback.xml resource is the canonical path; we don't ship one,
            // so root level stays at INFO (acceptable).
        }
        factoryClass.contains("org.slf4j.simple") -> {
            // slf4j-simple: standard config knobs.
            System.setProperty("org.slf4j.simpleLogger.defaultLogLevel", "info")
            System.setProperty("org.slf4j.simpleLogger.showDateTime", "true")
            System.setProperty("org.slf4j.simpleLogger.dateTimeFormat", "HH:mm:ss.SSS")
        }
    }
}
