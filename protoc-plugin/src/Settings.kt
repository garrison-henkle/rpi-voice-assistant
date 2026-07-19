/*
 * Copyright 2000-2025 JetBrains s.r.o. and contributors. Use of this source code is governed by the Apache 2.0 license.
 */

package org.jetbrains.amper.plugins.protobuf

import org.jetbrains.amper.plugins.Configurable

@Configurable
interface Settings {
    /**
     * `protoc` tool version to use.
     * The tool is currently downloaded from the corresponding maven artifact.
     */
    val protocVersion: String get() = "3.25.8"

    /**
     * `protoc-gen-pbandk-jvm` version to use. Distributed by pbandk as a
     * self-executing JAR from Maven Central (`pro.streem.pbandk:protoc-gen-pbandk-jvm`,
     * classifier `jvm8`). The plugin generates pure Kotlin (KMP-friendly) message
     * code; it does not emit gRPC stubs.
     */
    val pbandkVersion: String get() = "0.16.0"
}
