package nyc.durham.vira.companion

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/** Pairing credentials + sync state, in app-private SharedPreferences.
 *  Beta note, stated plainly: the token is stored unencrypted in this
 *  app's private storage. It only unlocks a hub that is reachable over
 *  the phone's own tailnet, and unpairing from the hub revokes it. */
class Store(ctx: Context) {
    private val p: SharedPreferences =
        ctx.getSharedPreferences("vira", Context.MODE_PRIVATE)

    var hubUrl: String
        get() = p.getString("hub_url", "") ?: ""
        set(v) = p.edit().putString("hub_url", v).apply()

    var deviceId: String
        get() = p.getString("device_id", "") ?: ""
        set(v) = p.edit().putString("device_id", v).apply()

    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v).apply()

    val paired: Boolean get() = hubUrl.isNotEmpty() && token.isNotEmpty()

    /** Newest SMS `date` (ms) already uploaded by the history backfill. */
    var smsWatermark: Long
        get() = p.getLong("sms_watermark", 0L)
        set(v) = p.edit().putLong("sms_watermark", v).apply()

    /** Last ping id the poller has shown. */
    var pingAfter: Long
        get() = p.getLong("ping_after", 0L)
        set(v) = p.edit().putLong("ping_after", v).apply()

    var uploadedTotal: Long
        get() = p.getLong("uploaded_total", 0L)
        set(v) = p.edit().putLong("uploaded_total", v).apply()

    /** Recent pings, newest last, for the Main screen list. */
    fun recentPings(): List<Pair<String, String>> {
        val raw = p.getString("recent_pings", "[]") ?: "[]"
        return try {
            val arr = JSONArray(raw)
            (0 until arr.length()).map {
                val o = arr.getJSONObject(it)
                o.optString("created") to o.optString("text")
            }
        } catch (_: Exception) { emptyList() }
    }

    fun addPings(pings: List<JSONObject>) {
        val keep = (recentPings().map { JSONObject().put("created", it.first)
            .put("text", it.second) } + pings).takeLast(20)
        val arr = JSONArray()
        keep.forEach { arr.put(it) }
        p.edit().putString("recent_pings", arr.toString()).apply()
    }

    fun unpair() {
        p.edit().remove("hub_url").remove("device_id").remove("token")
            .remove("ping_after").apply()
    }
}
