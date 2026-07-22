# Multi-stage build: invoke Amper's `:executableJarJvm` task in the builder,
# producing a self-contained Spring-Boot-loader-style fat jar. The runtime
# image then needs only the single jar; no classpath wildcard, no dedupe.
#
# This file must be invoked from the repo root as:
#     docker build -f Dockerfile -t rpi-assistant .

FROM eclipse-temurin:21-jdk AS builder
WORKDIR /src

# Metadata first so dependency resolution caches across rebuilds.
COPY project.yaml libs.versions.toml ./
COPY kotlin ./kotlin
COPY kotlin.module-template.yaml protobuf.module-template.yaml ./
COPY protoc-plugin ./protoc-plugin
COPY pbandk-id-codegen ./pbandk-id-codegen
COPY rpi-assistant ./rpi-assistant

# `:executableJarJvm` produces a self-contained ~128MB fat jar that
# `java -jar` runs without a classpath. No external /app/lib/ needed.
RUN --mount=type=cache,target=/root/.cache \
    sh ./kotlin task :rpi-assistant:executableJarJvm

FROM eclipse-temurin:21-jre AS runtime
WORKDIR /app

COPY --from=builder /src/build/tasks/_rpi-assistant_executableJarJvm/rpi-assistant-jvm-executable.jar /app/rpi-assistant.jar

ENV RPI_ASSISTANT_HTTP_PORT=6059 \
    RPI_LLM_BASE_URL=http://ollama:11434 \
    RPI_LLM_MODEL=qwen3-nest-mini \
    RPI_TTS_BASE_URL=http://piper:5000 \
    RPI_TTS_VOICE=en_US-lessac-medium

EXPOSE 6059

ENV JAVA_OPTS="-XX:+UseG1GC -XX:MaxRAMPercentage=70.0"
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/rpi-assistant.jar"]
