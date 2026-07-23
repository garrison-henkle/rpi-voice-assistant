package dev.henkle.rpi.assistant.tools

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/**
 * Outcome of running one tool. [text] is the short spoken ack the
 * orchestrator sends into Piper (chime-driven fast path so the user
 * hears something during the LLM's "reflect" turn) and optionally into
 * `text_delta` so any downstream UI can render it. [state] is an opaque
 * JSON payload (e.g. {"lights":{"kitchen":"on"}}) so future UIs can
 * sync state without yet another round-trip.
 */
data class ToolResult(val text: String, val state: JsonObject = JsonObject(emptyMap()))

/**
 * Canned tool executor. Stubs out the things Rhasspy needs until the
 * Koog tool layer lands:
 *   - lights.on { room } / lights.off { room }
 *   - weather.current { location }
 * Each returns a one-line ack so the satellite speaks immediately.
 *
 * The interface in [execute] is intentionally narrow — find/replace
 * this object with a real Koog/Home-Assistant/Weather-API implementation
 * later without touching HttpApi.kt.
 */
interface ToolExecutor {
    suspend fun execute(name: String, arguments: JsonObject): ToolResult

    /** OpenAI-shaped tool schemas; pass them straight into ollama `tools`. */
    fun schemas(): List<JsonObject>
}

class StubToolExecutor : ToolExecutor {
    override suspend fun execute(name: String, arguments: JsonObject): ToolResult {
        val room = (arguments["room"]?.toString() ?: "").trim('"').ifBlank { "room" }
        val location = (arguments["location"]?.toString() ?: "").trim('"').ifBlank { "here" }
        val cleanRoom = room.ifBlank { "the room" }
        return when (name) {
            "lights.on" -> ToolResult(
                text = "Turning on the $cleanRoom lights.",
                state = buildJsonObject {
                    put("light", buildJsonObject {
                        put("room", cleanRoom)
                        put("state", "on")
                    })
                },
            )
            "lights.off" -> ToolResult(
                text = "Turning off the $cleanRoom lights.",
                state = buildJsonObject {
                    put("light", buildJsonObject {
                        put("room", cleanRoom)
                        put("state", "off")
                    })
                },
            )
            "weather.current" -> ToolResult(
                text = "It looks like the temperature is around 68 degrees in $location, partly cloudy.",
                state = buildJsonObject {
                    put("weather", buildJsonObject {
                        put("location", location)
                        put("summary", "partly cloudy")
                        put("temp_f", 68)
                    })
                },
            )
            else -> ToolResult(text = "I don't know how to do that yet.")
        }
    }

    override fun schemas(): List<JsonObject> = listOf(
        buildJsonObject {
            put("type", "function")
            put("function", buildJsonObject {
                put("name", "lights.on")
                put("description", "Turn on a named set of lights.")
                put("parameters", buildJsonObject {
                    put("type", "object")
                    put("properties", buildJsonObject {
                        putJsonObject("room") {
                            put("type", "string")
                            put("description", "Room name, e.g. kitchen, bedroom")
                        }
                    })
                    putJsonArray("required") { add("room") }
                })
            })
        },
        buildJsonObject {
            put("type", "function")
            put("function", buildJsonObject {
                put("name", "lights.off")
                put("description", "Turn off a named set of lights.")
                put("parameters", buildJsonObject {
                    put("type", "object")
                    put("properties", buildJsonObject {
                        putJsonObject("room") {
                            put("type", "string")
                            put("description", "Room name, e.g. kitchen, bedroom")
                        }
                    })
                    putJsonArray("required") { add("room") }
                })
            })
        },
        buildJsonObject {
            put("type", "function")
            put("function", buildJsonObject {
                put("name", "weather.current")
                put("description", "Get the current weather for a location.")
                put("parameters", buildJsonObject {
                    put("type", "object")
                    put("properties", buildJsonObject {
                        putJsonObject("location") {
                            put("type", "string")
                            put("description", "City or place name")
                        }
                    })
                    putJsonArray("required") { add("location") }
                })
            })
        },
    )
}
