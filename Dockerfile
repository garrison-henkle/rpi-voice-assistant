# --- builder: produces the project's classes + collects runtime deps ---
FROM eclipse-temurin:21-jdk AS builder
WORKDIR /src
COPY project.yaml libs.versions.toml kotlin.module-template.yaml \
      protobuf.module-template.yaml kotlin kotlin.bat ./
COPY protoc-plugin ./protoc-plugin
COPY pbandk-id-codegen ./pbandk-id-codegen
COPY rpi-assistant ./rpi-assistant

# Build the proto ServiceGenerator jar first; the rpi-assistant module's
# protoc plumbing reads it via the build-dir task output.
RUN --mount=type=cache,target=/root/.m2 \
    sh ./kotlin task :pbandk-id-codegen:jarJvm
RUN --mount=type=cache,target=/root/.m2 \
    sh ./kotlin task :rpi-assistant:jarJvm

# Amper's :jarJvm task emits a thin jar (project classes only). Snapshot every
# dependency the resolver pulled so we can put them on the runtime classpath.
RUN mkdir -p /out/app /out/lib && \
    cp build/tasks/_pbandk-id-codegen_jarJvm/pbandk-id-codegen-jvm.jar /out/lib/ 2>/dev/null || true; \
    cp build/tasks/_rpi-assistant_jarJvm/rpi-assistant-jvm.jar          /out/app/

RUN find /root/.m2 -name '*.jar' \
        -not -name '*sources.jar' \
        -not -name '*javadoc.jar' \
        -exec cp {} /out/lib/ \; 2>/dev/null || true

# --- runtime: minimal JRE + the produced jar + lib/ on the classpath ---
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=builder /out/app/rpi-assistant-jvm.jar /app/rpi-assistant.jar
COPY --from=builder /out/lib/                       /app/lib/

ENV RPI_ORCHESTRATOR_PORT=6053 \
    RPI_LLM_BASE_URL=http://ollama:11434 \
    RPI_LLM_MODEL=qwen3-nest-mini \
    RPI_TTS_BASE_URL=http://piper:5000 \
    RPI_TTS_VOICE=en_US-lessac-medium

EXPOSE 6053
ENTRYPOINT ["java", "-XX:+UseG1GC", "-XX:MaxRAMPercentage=70.0", \
    "-cp", "/app/rpi-assistant.jar:/app/lib/*", \
    "dev.henkle.rpi.assistant.AssistantKt"]
