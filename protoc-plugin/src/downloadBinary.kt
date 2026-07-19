/*
 * Copyright 2000-2026 JetBrains s.r.o. and contributors. Use of this source code is governed by the Apache 2.0 license.
 */

package org.jetbrains.amper.plugins.protobuf

import java.io.IOException
import java.net.URI
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermission
import kotlin.io.path.createDirectories
import kotlin.io.path.getPosixFilePermissions
import kotlin.io.path.inputStream
import kotlin.io.path.outputStream
import kotlin.io.path.setPosixFilePermissions

/**
 * Naive toy artifact downloader.
 */
context(systemInfo: SystemInfo)
fun downloadBinary(
    group: String,
    name: String,
    version: String,
    to: Path,
) {
    val path = buildList {
        addAll(group.splitToSequence('.'))
        add(name)
    }.joinToString("/")

    val url = "https://repo1.maven.org/maven2/$path/$version/$name-$version-" +
            "${systemInfo.os.string}-${systemInfo.arch.string}.exe"
    try {
        URI(url).toURL().openStream().buffered(64 * 1024).use { input ->
            to.outputStream().use {
                input.copyTo(it)
            }
        }
        when (systemInfo.os) {
            SystemInfo.Os.Linux, SystemInfo.Os.Mac -> {
                to.setPosixFilePermissions(to.getPosixFilePermissions() + PosixFilePermission.OWNER_EXECUTE)
            }
            SystemInfo.Os.Windows -> {}
        }
    } catch (e: IOException) {
        error("Unable to download $url: ${e.message}")
    }
}

/**
 * Downloads the well-known `.proto` files (`google/protobuf/descriptor.proto`,
 * `wrappers.proto`, etc.) by extracting them from the `protobuf-java` JAR on
 * Maven Central. The protoc executable downloaded via [downloadBinary] does
 * not include these proto sources; protoc requires them on disk to resolve
 * `import "google/protobuf/..."`.
 */
context(systemInfo: SystemInfo)
fun downloadIncludeProtos(
    version: String,
    to: Path,
) {
    val url = "https://repo1.maven.org/maven2/com/google/protobuf/protobuf-java/$version/" +
            "protobuf-java-$version.jar"
    val tempFile = java.nio.file.Files.createTempFile("protobuf-java-", ".jar")
    try {
        URI(url).toURL().openStream().buffered(64 * 1024).use { input ->
            tempFile.outputStream().use { input.copyTo(it) }
        }
        extractProtosFromJar(tempFile, to = to)
    } catch (e: IOException) {
        error("Unable to download $url: ${e.message}")
    } finally {
        java.nio.file.Files.deleteIfExists(tempFile)
    }
}

/**
 * Walks a `protobuf-java` JAR and copies every `.proto` file under
 * `google/protobuf/` into [to], preserving the relative path so protoc's
 * `import "google/protobuf/descriptor.proto"` lookups succeed.
 */
private fun extractProtosFromJar(jar: Path, to: Path) {
    to.createDirectories()
    val marker = "google/protobuf/"
    java.util.zip.ZipInputStream(jar.inputStream().buffered()).use { zis ->
        while (true) {
            val entry = zis.nextEntry ?: break
            if (entry.isDirectory) continue
            val name = entry.name
            if (!name.startsWith(marker) || !name.endsWith(".proto")) continue
            val target = to.resolve(name)
            target.parent?.createDirectories()
            target.outputStream().use { out -> zis.copyTo(out) }
        }
    }
}

/**
 * Downloads the `protoc-gen-pbandk-jvm` self-executing JAR (Maven Central,
 * classifier `jvm8`) and installs it alongside the protoc binary as
 * `protoc-gen-pbandk`, with execute permission on Linux/Mac. pbandk's
 * generator emits pure-Kotlin (KMP-ready) message code; gRPC stubs are not
 * produced (see Settings.kt for details).
 *
 * The classifier-suffixed JAR is renamed rather than referenced via its
 * full Maven filename because protoc looks up `protoc-gen-pbandk` (or the
 * `--plugin=protoc-gen-pbandk=...` override we use) on disk. The JAR is
 * self-executing (it embeds a `Main-Class` manifest entry), so it can be
 * invoked directly without a `java -jar` wrapper.
 *
 * On Windows, the rename is a no-op for execution but we still rename to a
 * stable filename so the `--plugin=...` path is consistent across OSes.
 */
context(systemInfo: SystemInfo)
fun downloadPbandkPlugin(
    version: String,
    to: Path,
) {
    val finalName = "protoc-gen-pbandk"
    val tempFile = Files.createTempFile("protoc-gen-pbandk-", ".jar")
    val url = "https://repo1.maven.org/maven2/pro/streem/pbandk/protoc-gen-pbandk-jvm/$version/" +
            "protoc-gen-pbandk-jvm-$version-jvm8.jar"
    try {
        URI(url).toURL().openStream().buffered(64 * 1024).use { input ->
            tempFile.outputStream().use { input.copyTo(it) }
        }
        Files.move(tempFile, to.resolve(finalName), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        to.resolve(finalName).setPosixFilePermissionsIfPossible(
            systemInfo,
            PosixFilePermission.OWNER_EXECUTE,
        )
    } catch (e: IOException) {
        error("Unable to download $url: ${e.message}")
    } finally {
        Files.deleteIfExists(tempFile)
    }
}

private fun Path.setPosixFilePermissionsIfPossible(
    systemInfo: SystemInfo,
    vararg perms: PosixFilePermission,
) {
    if (systemInfo.os == SystemInfo.Os.Windows) return
    setPosixFilePermissions(getPosixFilePermissions() + perms.toSet())
}
