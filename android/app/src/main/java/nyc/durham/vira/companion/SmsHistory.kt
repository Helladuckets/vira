package nyc.durham.vira.companion

import android.content.Context
import android.provider.Telephony

/** SMS history backfill straight from the provider, both directions,
 *  oldest first past a watermark so an interrupted run resumes where it
 *  stopped. Runs on a worker thread; progress lands on [onProgress]. */
object SmsHistory {

    fun count(ctx: Context): Int {
        return ctx.contentResolver.query(
            Telephony.Sms.CONTENT_URI, arrayOf("_id"),
            null, null, null)?.use { it.count } ?: 0
    }

    /** Recent inbound bodies — the SMS-echo check for the notification
     *  capture (NotifRules.isSmsEcho). */
    fun recentInboundBodies(ctx: Context, limit: Int = 5): List<String> {
        val out = mutableListOf<String>()
        try {
            ctx.contentResolver.query(
                Telephony.Sms.CONTENT_URI,
                arrayOf(Telephony.Sms.BODY),
                "${Telephony.Sms.TYPE} = ?",
                arrayOf(Telephony.Sms.MESSAGE_TYPE_INBOX.toString()),
                "${Telephony.Sms.DATE} DESC LIMIT $limit")?.use { c ->
                while (c.moveToNext()) out.add(c.getString(0) ?: "")
            }
        } catch (_: Exception) { return emptyList() }
        return out
    }

    /** Upload everything newer than the watermark in provider batches.
     *  Returns the number of messages handed to the queue. */
    fun backfill(ctx: Context, store: Store,
                 onProgress: (done: Int, total: Int) -> Unit): Int {
        val resolver = ctx.contentResolver
        var handed = 0
        val sel = "${Telephony.Sms.DATE} > ?"
        val total = resolver.query(
            Telephony.Sms.CONTENT_URI, arrayOf("_id"), sel,
            arrayOf(store.smsWatermark.toString()), null)?.use { it.count } ?: 0
        while (true) {
            val batch = mutableListOf<Msg>()
            var newest = store.smsWatermark
            resolver.query(
                Telephony.Sms.CONTENT_URI,
                arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY,
                        Telephony.Sms.DATE, Telephony.Sms.TYPE),
                sel, arrayOf(store.smsWatermark.toString()),
                "${Telephony.Sms.DATE} ASC LIMIT 200")?.use { c ->
                while (c.moveToNext()) {
                    val addr = c.getString(0) ?: continue
                    val body = c.getString(1) ?: continue
                    val date = c.getLong(2)
                    val type = c.getInt(3)
                    if (body.isBlank()) continue
                    val direction = when (type) {
                        Telephony.Sms.MESSAGE_TYPE_INBOX -> "in"
                        Telephony.Sms.MESSAGE_TYPE_SENT -> "out"
                        else -> continue        // drafts, outbox, failed
                    }
                    batch.add(Msg(addr, body, date, "sms", direction, "history"))
                    if (date > newest) newest = date
                }
            }
            if (batch.isEmpty()) break
            batch.forEach { UploadQueue.enqueue(ctx, it) }
            handed += batch.size
            store.smsWatermark = newest
            onProgress(handed, total)
        }
        return handed
    }
}
