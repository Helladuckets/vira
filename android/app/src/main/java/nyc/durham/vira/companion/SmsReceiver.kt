package nyc.durham.vira.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/** Live SMS as it arrives (multipart texts are joined per sender). The
 *  provider backfill would sweep these up eventually; the broadcast just
 *  makes the feed live. The hub's near-dupe window absorbs the overlap. */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        val bySender = parts.filterNotNull().groupBy {
            it.originatingAddress ?: ""
        }
        for ((sender, msgs) in bySender) {
            if (sender.isEmpty()) continue
            val body = msgs.joinToString("") { it.messageBody ?: "" }
            if (body.isBlank()) continue
            UploadQueue.enqueue(ctx, Msg(
                sender = sender,
                text = body,
                whenMs = msgs.first().timestampMillis,
                channel = "sms",
                direction = "in",
                source = "live"))
        }
    }
}
