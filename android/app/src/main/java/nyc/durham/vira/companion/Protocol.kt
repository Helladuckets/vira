package nyc.durham.vira.companion

import org.json.JSONArray
import org.json.JSONObject

/** One captured message, exactly what the hub ingests. Pure data — no
 *  Android imports, so the protocol round-trips under plain-JVM tests. */
data class Msg(
    val sender: String,
    val text: String,
    val whenMs: Long,
    val channel: String,    // sms | mms | rcs | whatsapp | notification
    val direction: String,  // in | out
    val source: String,     // history | live
) {
    fun toJson(): JSONObject = JSONObject()
        .put("sender", sender)
        .put("text", text)
        .put("when", whenMs)
        .put("channel", channel)
        .put("direction", direction)
        .put("source", source)

    companion object {
        fun fromJson(o: JSONObject): Msg = Msg(
            sender = o.optString("sender"),
            text = o.optString("text"),
            whenMs = o.optLong("when"),
            channel = o.optString("channel", "sms"),
            direction = o.optString("direction", "in"),
            source = o.optString("source", "live"),
        )

        fun batchJson(msgs: List<Msg>): JSONObject {
            val arr = JSONArray()
            msgs.forEach { arr.put(it.toJson()) }
            return JSONObject().put("messages", arr)
        }
    }
}

/** The pairing QR / paste-text payload the hub mints:
 *  {"v":1,"kind":"vira-pair","url":...,"device_id":...,"token":...} */
data class PairPayload(val url: String, val deviceId: String, val token: String) {
    companion object {
        fun parse(text: String?): PairPayload? {
            val raw = text?.trim() ?: return null
            if (raw.isEmpty()) return null
            val o = try { JSONObject(raw) } catch (_: Exception) { return null }
            if (o.optString("kind") != "vira-pair") return null
            val url = o.optString("url").trim().trimEnd('/')
            val id = o.optString("device_id")
            val token = o.optString("token")
            if (url.isEmpty() || id.isEmpty() || token.isEmpty()) return null
            if (!url.startsWith("http://") && !url.startsWith("https://")) return null
            return PairPayload(url, id, token)
        }
    }
}

/** Notification-capture rules, kept pure for tests.
 *
 *  The Messages app (com.google.android.apps.messaging) posts
 *  notifications for BOTH SMS and RCS. SMS already arrives through the
 *  provider paths with the real phone number; the notification only
 *  carries a display name. So: when we can read the SMS provider and the
 *  body is there, the notification is an SMS echo — skip it. When it is
 *  not there (or we cannot look), it is RCS (or the only door we have) —
 *  capture it. */
object NotifRules {
    const val PKG_MESSAGES = "com.google.android.apps.messaging"
    const val PKG_WHATSAPP = "com.whatsapp"
    const val PKG_WHATSAPP_BIZ = "com.whatsapp.w4b"

    val CAPTURE_PACKAGES = setOf(PKG_MESSAGES, PKG_WHATSAPP, PKG_WHATSAPP_BIZ)

    private val SUMMARY_RX = Regex(
        """^\s*\d+\s+new\s+messages?\s*$|^Checking for new messages$""",
        RegexOption.IGNORE_CASE)

    fun channelFor(pkg: String): String = when (pkg) {
        PKG_WHATSAPP, PKG_WHATSAPP_BIZ -> "whatsapp"
        PKG_MESSAGES -> "rcs"
        else -> "notification"
    }

    /** Group-summary style bodies carry no message content. */
    fun isSummaryText(title: String?, text: String?): Boolean {
        if (title.isNullOrBlank() || text.isNullOrBlank()) return true
        return SUMMARY_RX.matches(text.trim())
    }

    /** The SMS-echo rule described above. `recentSmsBodies` is the last
     *  few inbound provider rows when READ_SMS is granted, else null. */
    fun isSmsEcho(pkg: String, text: String, recentSmsBodies: List<String>?): Boolean {
        if (pkg != PKG_MESSAGES) return false
        if (recentSmsBodies == null) return false
        return recentSmsBodies.any { it.trim() == text.trim() }
    }
}
