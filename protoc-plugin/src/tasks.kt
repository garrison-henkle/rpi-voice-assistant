/*
 * Copyright 2000-2025 JetBrains s.r.o. and contributors. Use of this source code is governed by the Apache 2.0 license.
 */

package org.jetbrains.amper.plugins.protobuf

import org.jetbrains.amper.plugins.Input
import org.jetbrains.amper.plugins.Output
import org.jetbrains.amper.plugins.TaskAction
import java.nio.file.Path
import kotlin.io.path.createDirectories
import kotlin.io.path.deleteRecursively
import kotlin.io.path.div
import kotlin.io.path.exists
import kotlin.io.path.extension
import kotlin.io.path.pathString
import kotlin.io.path.walk

@TaskAction
fun provisionBinaries(
    settings: Settings,
    @Output bin: Path,
    @Output include: Path,
) {
    val protocVersion = settings.protocVersion

    bin.createDirectories()
    include.createDirectories()

    context(SystemInfo.detect()) {
        downloadBinary("com.google.protobuf", "protoc", protocVersion, to = bin / PROTOC_BINARY)
        downloadPbandkPlugin(settings.pbandkVersion, to = bin)
        downloadIncludeProtos(protocVersion, to = include)
    }
}

@TaskAction
fun generateProto(
    @Input bin: Path,
    @Input include: Path,
    @Input sourceDir: Path,
    @Input codegenJar: Path,
    @Output kotlinOutputDir: Path,
) {
    val protoc = bin / PROTOC_BINARY
    val pbandkPlugin = bin / PBANDK_BINARY

    kotlinOutputDir.apply {
        deleteRecursively()
        createDirectories()
    }

    val protoFiles = sourceDir.walk().filter { it.extension == "proto" }.sorted().toList()
    if (protoFiles.isEmpty()) {
        return
    }

    if (!codegenJar.exists()) {
        error(
            "pbandk ServiceGenerator plugin jar is missing at: $codegenJar\n" +
                "Run `./kotlin task :pbandk-id-codegen:jarJvm` to build it, " +
                "then rerun `./kotlin task :rpi-assistant:runJvm`.",
        )
    }

    val commandLine = buildList {
        add(protoc.pathString)
        add("--plugin=protoc-gen-pbandk=$pbandkPlugin")
        add(
            "--pbandk_out=" +
                "kotlin_service_gen=${codegenJar.toAbsolutePath()}" +
                "|dev.henkle.rpi.assistant.gen.EsphomeMessageIdGenerator" +
                ":$kotlinOutputDir",
        )
        add("-I$sourceDir")
        add("-I$include")
        addAll(protoFiles.map { it.pathString })
    }

    ProcessBuilder(commandLine)
        .inheritIO()
        .start()
        .waitFor()
        .let {
            check(it == 0) {
                "protoc terminated with code = $it. See the log for the errors"
            }
        }
}

private const val PROTOC_BINARY = "protoc.exe"
private const val PBANDK_BINARY = "protoc-gen-pbandk"
