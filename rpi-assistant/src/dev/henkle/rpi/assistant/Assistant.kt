package dev.henkle.rpi.assistant

import ch.qos.logback.classic.Level
import ch.qos.logback.classic.Logger
import ch.qos.logback.classic.encoder.PatternLayoutEncoder
import ch.qos.logback.classic.spi.ILoggingEvent
import ch.qos.logback.core.ConsoleAppender
import dev.henkle.rpi.assistant.llm.OllamaKoogBridge
import dev.henkle.rpi.assistant.tts.PiperTtsHttp
import org.slf4j.LoggerFactory

fun main() {
    configureLogging()
    val log = LoggerFactory.getLogger("rpi-assistant")
    val cfg = AssistantConfig.fromEnv()
    log.info("Starting rpi-assistant on :{}", cfg.orchestratorPort)
    log.info("LLM: model='{}' baseUrl='{}'", cfg.ollamaModel, cfg.ollamaBaseUrl)
    log.info("TTS: voice='{}' baseUrl='{}'", cfg.piperVoice, cfg.piperBaseUrl)

    val llm = OllamaKoogBridge(cfg.ollamaBaseUrl, cfg.ollamaModel)
    val tts = PiperTtsHttp(cfg.piperBaseUrl, cfg.piperVoice)
    val orchestrator = LocalVoiceOrchestrator(
        port = cfg.orchestratorPort,
        llm = llm,
        tts = tts,
        log = log,
    )

    Runtime.getRuntime().addShutdownHook(Thread {
        log.info("Shutdown signal received; stopping orchestrator")
        runCatching { orchestrator.stop() }
        runCatching { tts.close() }
        runCatching { llm.close() }
    })
    orchestrator.start()
}

/**
 * Wire Logback programmatically so we don't depend on a classpath resource
 * being discovered by Amper's jvm/app packaging. Pattern matches the rest of
 * the Kotlin ecosystem and writes to stderr (= Logback default).
 */
private fun configureLogging() {
    val lc = LoggerFactory.getILoggerFactory() as ch.qos.logback.classic.LoggerContext
    val root = lc.getLogger(Logger.ROOT_LOGGER_NAME)
    root.level = Level.INFO
    val encoder = PatternLayoutEncoder().apply {
        context = lc
        pattern = "%d{HH:mm:ss.SSS} %-5level [%thread] %logger{36} - %msg%n"
        start()
    }
    val appender = ConsoleAppender<ILoggingEvent>().apply {
        context = lc
        this.encoder = encoder
        start()
    }
    root.addAppender(appender)
    // silence the noisy Ktor / HttpURLConnection defaults
    lc.getLogger("io.ktor").level = Level.WARN
    lc.getLogger("io.netty").level = Level.WARN
}
