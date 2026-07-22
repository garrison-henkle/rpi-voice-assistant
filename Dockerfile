# Single-stage runtime image. The Kotlin jar is built *outside* Docker
# (`sh ./kotlin task :pbandk-id-codegen:jarJvm` and
# `sh ./kotlin task :rpi-assistant:jarJvm`) and the resolved dep jars are
# staged into ./app-deps/. Both folders must exist before `docker compose build`.
# This avoids running protoc inside Docker's sandboxed network, which fails
# to fetch the protoc binary and Maven Central plugin jars in offline hosts.
FROM eclipse-temurin:21-jre AS runtime
WORKDIR /app

COPY build/tasks/_rpi-assistant_jarJvm/rpi-assistant-jvm.jar            /app/rpi-assistant.jar
COPY build/tasks/_pbandk-id-codegen_jarJvm/pbandk-id-codegen-jvm.jar    /app/lib/
COPY app-deps/                                                           /app/lib/

ENV RPI_ORCHESTRATOR_PORT=6059 \
    RPI_LLM_BASE_URL=http://ollama:11434 \
    RPI_LLM_MODEL=qwen3-nest-mini \
    RPI_TTS_BASE_URL=http://piper:5000 \
    RPI_TTS_VOICE=en_US-lessac-medium

EXPOSE 6059
ENTRYPOINT ["java", "-XX:+UseG1GC", "-XX:MaxRAMPercentage=70.0", \
    "-cp", "/app/rpi-assistant.jar:/app/lib/*", \
    "dev.henkle.rpi.assistant.AssistantKt"]
