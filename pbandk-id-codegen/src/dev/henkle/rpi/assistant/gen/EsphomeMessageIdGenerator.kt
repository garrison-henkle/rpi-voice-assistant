/*
 * Custom pbandk `protoc-gen-pbandk` ServiceGenerator that emits one Kotlin
 * extension file per proto file's destination package, exposing each ESPHome
 * message's wire-protocol fields as Companion-object extension properties:
 *
 *     public val HelloRequest.Companion.ID: UInt get() = 1u
 *     public val HelloRequest.Companion.APISource: dev.henkle.rpi.assistant.proto.APISourceType
 *         get() = APISourceType.SOURCE_CLIENT
 *
 * Loaded by protoc-gen-pbandk via the
 *   --pbandk_out=kotlin_service_gen=<jar>|dev.henkle.rpi.assistant.gen.EsphomeMessageIdGenerator:...
 * option. The ESPHome `(id)` and `(source)` options live on
 * `google.protobuf.MessageOptions` but pbandk only ships the standard
 * `descriptor.proto` wkt, so on `pbandk.wkt.MessageOptions` they appear in
 * `unknownFields` (field numbers 1036/1037, varint wire type) — we decode
 * the raw bytes directly here, avoiding the need to construct typed
 * `FieldDescriptor`s for the ESPHome extensions.
 */

package dev.henkle.rpi.assistant.gen

import pbandk.UnknownField
import pbandk.gen.ServiceGenerator
import pbandk.wkt.DescriptorProto
import pbandk.wkt.FileDescriptorProto
import pbandk.wkt.MessageOptions

class EsphomeMessageIdGenerator : ServiceGenerator {

    override fun generate(service: ServiceGenerator.Service): List<ServiceGenerator.Result> {
        val byPackage = mutableMapOf<String, MutableList<MessageIdEntry>>()

        service.rawRequest.protoFile.forEach { file ->
            val pkg = kotlinPackageNameOf(file)
            val sink: (MessageIdEntry) -> Unit = { entry ->
                byPackage.getOrPut(pkg) { mutableListOf() }.add(entry)
            }
            file.messageType.forEach { walk(file, it, sink) }
        }

        return byPackage.map { (pkg, entries) ->
            ServiceGenerator.Result(
                code = renderExtensionFile(pkg, entries),
                otherFilePath = pkg.replace('.', '/') + "/MessageIdExtensions.kt",
                otherFileInsertionPoint = null,
            )
        }
    }

    private fun walk(
        file: FileDescriptorProto,
        msg: DescriptorProto,
        emit: (MessageIdEntry) -> Unit,
    ) {
        val msgName: String = msg.name ?: return
        val opts: MessageOptions = msg.options ?: return
        val ufs: Map<Int, UnknownField> = opts.unknownFields
        val id = ufs[FIELD_ID]?.values?.firstOrNull()?.let(::decodeUInt32Varint)
            ?.takeIf { it > 0u }
        val APISource = ufs[FIELD_SOURCE]?.values?.firstOrNull()
            ?.let(::decodeUInt32Varint)?.toInt()
            ?.let(::sourceNameOf)
        if (id != null) emit(MessageIdEntry(msgName, id, APISource))
        msg.nestedType.forEach { walk(file, it, emit) }
    }

    private fun renderExtensionFile(pkg: String, entries: List<MessageIdEntry>): String =
        buildString {
            appendLine("@file:OptIn(pbandk.PublicForGeneratedCode::class)")
            appendLine()
            appendLine("package $pkg")
            appendLine()
            appendLine("import kotlin.UInt")
            appendLine()
            entries.forEach { e ->
                appendLine("public val ${e.messageName}.Companion.ID: UInt get() = ${e.id}u")
                if (e.APISource != null) {
                    appendLine("public val ${e.messageName}.Companion.APISource: $pkg.APISourceType")
                    appendLine("    get() = $pkg.APISourceType.${e.APISource}")
                }
            }
        }.trimEnd() + "\n"

    private data class MessageIdEntry(
        val messageName: String,
        val id: UInt,
        val APISource: String?,
    )

    // pbandk's destination-Kotlin-package rule for a .proto file:
    //   1. file.options.kotlinPackage   (rare; not exposed in descriptor)
    //   2. file.options.javaPackage     (set in ESPHome esphome.proto)
    //   3. file.package                 (raw proto package, dot-separated)
    private fun kotlinPackageNameOf(file: FileDescriptorProto): String {
        val javaPkg: String? = file.options?.javaPackage?.takeUnless { it.isNullOrBlank() }
        val protoPkg: String? = file.`package`
        val raw: String? = javaPkg ?: protoPkg
        if (raw.isNullOrBlank()) return "default"
        return raw.split('.').joinToString(".")
    }

    private fun decodeUInt32Varint(value: UnknownField.Value): UInt {
        var result = 0u
        var shift = 0
        for (b in value.rawBytes.array) {
            result = result or ((b.toUInt() and 0x7Fu) shl shift)
            if ((b.toInt() and 0x80) == 0) break
            shift += 7
        }
        return result
    }

    private fun sourceNameOf(value: Int): String? = when (value) {
        1 -> "SOURCE_SERVER"
        2 -> "SOURCE_CLIENT"
        else -> null // 0 = SOURCE_BOTH (default) or unknown — drop the extension
    }

    private companion object {
        // Numbers declared in esphome_options.proto:
        //   extend google.protobuf.MessageOptions {
        //     optional uint32 id = 1036 [default=0];
        //     optional APISourceType source = 1037 [default=SOURCE_BOTH];
        //   }
        const val FIELD_ID = 1036
        const val FIELD_SOURCE = 1037
    }
}
