# Multi-stage build: build the Kotlin jar inside the container, then copy
# the resulting jars into a minimal JRE runtime image. The `--mount=type=cache`
# in the builder stage keeps Amper + Maven + Gradle caches between rebuilds,
# so a second `docker compose build` skips the cold re-download.
#
# This file must be invoked from the repo root as:
#     docker build -f Dockerfile -t rpi-assistant .
#
# Network: requires host-side internet access during the builder stage so
# Amper can resolve koog/ktor/etc. Maven artifacts. This is the previously-
# broken step on the Pi; it now works because we no longer pull the protoc
# binary + pbandk plugin (the pbandk + protobuf layers were dropped).

FROM eclipse-temurin:21-jdk AS builder
WORKDIR /src

# Copy only the metadata first so the dependency resolution layer caches.
COPY project.yaml libs.versions.toml ./
COPY kotlin ./kotlin
COPY kotlin.module-template.yaml protobuf.module-template.yaml ./
COPY protoc-plugin ./protoc-plugin
COPY pbandk-id-codegen ./pbandk-id-codegen
COPY rpi-assistant ./rpi-assistant

RUN --mount=type=cache,target=/root/.cache \
    sh ./kotlin task :rpi-assistant:jarJvm

# Collect every non-sources/javadoc jar Amper resolved into /src/deps/ so the
# runtime stage can copy them across. Multiple runs get overlapped, but the
# last write wins per path; that's idempotent.
RUN --mount=type=cache,target=/root/.cache \
    mkdir -p /src/deps && \
    find /root/.cache -name '*.jar' \
         -not -name '*-sources.jar' \
         -not -name '*-javadoc.jar' \
         -exec cp --update=none {} /src/deps/ \; 2>/dev/null || true

FROM eclipse-temurin:21-jre AS runtime
WORKDIR /app

COPY --from=builder /src/build/tasks/_rpi-assistant_jarJvm/rpi-assistant-jvm.jar /app/rpi-assistant.jar
COPY --from=builder /src/deps                                                          /app/lib/

ENV RPI_ASSISTANT_HTTP_PORT=6059 \
    RPI_LLM_BASE_URL=http://ollama:11434 \
    RPI_LLM_MODEL=qwen3-nest-mini \
    RPI_TTS_BASE_URL=http://piper:5000 \
    RPI_TTS_VOICE=en_US-lessac-medium

EXPOSE 6059

ENV JAVA_OPTS="-XX:+UseG1GC -XX:MaxRAMPercentage=70.0"
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -cp /app/rpi-assistant.jar:/app/lib/* dev.henkle.rpi.assistant.AssistantKt"]
