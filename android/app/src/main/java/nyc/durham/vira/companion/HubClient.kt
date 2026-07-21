package nyc.durham.vira.companion

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import org.json.JSONObject

/** Plain HttpURLConnection against the hub — synchronous, call from a
 *  worker thread. No client library: three endpoints do not earn one. */
class HubClient(private val store: Store) {

    class HubError(val code: Int, msg: String) : IOException(msg)

    private fun open(path: String, timeoutMs: Int): HttpURLConnection {
        val con = URL(store.hubUrl + path).openConnection() as HttpURLConnection
        con.connectTimeout = 10_000
        con.readTimeout = timeoutMs
        if (store.token.isNotEmpty()) {
            con.setRequestProperty("X-Vira-Device", store.deviceId)
            con.setRequestProperty("Authorization", "Bearer " + store.token)
        }
        return con
    }

    private fun readBody(con: HttpURLConnection): String {
        val stream = if (con.responseCode < 400) con.inputStream
                     else (con.errorStream ?: con.inputStream)
        return stream.readBytes().toString(StandardCharsets.UTF_8)
    }

    private fun request(method: String, path: String, body: JSONObject?,
                        timeoutMs: Int = 20_000): JSONObject {
        val con = open(path, timeoutMs)
        try {
            con.requestMethod = method
            if (body != null) {
                con.doOutput = true
                con.setRequestProperty("Content-Type", "application/json")
                con.outputStream.use {
                    it.write(body.toString().toByteArray(StandardCharsets.UTF_8))
                }
            }
            val text = readBody(con)
            if (con.responseCode >= 400)
                throw HubError(con.responseCode,
                    "hub said ${con.responseCode}: ${text.take(200)}")
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            con.disconnect()
        }
    }

    /** Claim the pairing the QR carried. On success the caller persists
     *  url/id/token from the payload. */
    fun pair(payload: PairPayload, deviceName: String, platform: String): JSONObject {
        val con = URL(payload.url + "/api/companion/pair")
            .openConnection() as HttpURLConnection
        con.connectTimeout = 10_000
        con.readTimeout = 15_000
        try {
            con.requestMethod = "POST"
            con.doOutput = true
            con.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject()
                .put("device_id", payload.deviceId)
                .put("token", payload.token)
                .put("name", deviceName)
                .put("platform", platform)
            con.outputStream.use {
                it.write(body.toString().toByteArray(StandardCharsets.UTF_8))
            }
            val text = readBody(con)
            if (con.responseCode >= 400)
                throw HubError(con.responseCode,
                    "pairing refused (${con.responseCode}): ${text.take(200)}")
            return JSONObject(text)
        } finally {
            con.disconnect()
        }
    }

    /** Push one batch; returns the hub's counts {received,new,duplicates}. */
    fun push(msgs: List<Msg>): JSONObject =
        request("POST", "/api/companion/messages", Msg.batchJson(msgs))

    /** Long-poll pings newer than [after]; blocks up to [waitS] server-side. */
    fun pings(after: Long, waitS: Int): JSONObject =
        request("GET", "/api/companion/pings?after=$after&wait=$waitS",
                null, timeoutMs = (waitS + 15) * 1000)
}
