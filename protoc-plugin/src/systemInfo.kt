/*
 * Copyright 2000-2025 JetBrains s.r.o. and contributors. Use of this source code is governed by the Apache 2.0 license.
 */

package org.jetbrains.amper.plugins.protobuf

/**
 * Native system info provider utility.
 * Uses [System.getProperty] (always available on the JVM) to avoid any
 * native dependency just for OS/arch detection.
 */
class SystemInfo private constructor(
    val os: Os,
    val arch: Arch,
) {
    enum class Os(val string: String) {
        Windows("windows"),
        Linux("linux"),
        Mac("osx"),
    }

    enum class Arch(val string: String) {
        AArch64("aarch_64"),
        X86_64("x86_64"),
    }

    companion object {
        fun detect(): SystemInfo {
            val osName = System.getProperty("os.name").lowercase()
            val os = when {
                osName.startsWith("mac") -> Os.Mac
                osName.startsWith("linux") -> Os.Linux
                osName.startsWith("windows") -> Os.Windows
                else -> error("Unsupported platform for protoc: $osName")
            }
            val rawArch = System.getProperty("os.arch").lowercase()
            val arch = when (rawArch) {
                "aarch64", "arm64" -> Arch.AArch64
                "x86_64", "amd64" -> Arch.X86_64
                else -> error("Unsupported architecture: $rawArch")
            }
            return SystemInfo(os, arch)
        }
    }
}
