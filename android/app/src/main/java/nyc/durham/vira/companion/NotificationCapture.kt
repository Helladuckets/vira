package nyc.durham.vira.companion

import android.app.Notification
import android.content.pm.PackageManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/** The only door Android leaves open to RCS and WhatsApp content: their
 *  notifications. This listener captures sender/text/timestamp from the
 *  messaging apps in NotifRules.CAPTURE_PACKAGES — nothing else is even
 *  looked at — and hands them to the upload queue.
 *
 *  It also hosts the ping long-poll (Pings.ensureRunning): notification
 *  access keeps this service alive, so pings arrive with the app closed
 *  — the no-Google-push design. */
class NotificationCapture : NotificationListenerService() {

    // (pkg, title, text) recently seen — notifications re-post on group
    // updates; the hub's near-dupe would catch these, this just saves the
    // round trip.
    private val seen = ArrayDeque<Pair<String, Long>>()

    override fun onListenerConnected() {
        UploadQueue.init(this)
        Pings.ensureRunning(this)
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg !in NotifRules.CAPTURE_PACKAGES) return
        if (sbn.isOngoing) return
        val n = sbn.notification ?: return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return

        val extras = n.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
        if (NotifRules.isSummaryText(title, text)) return

        val smsBodies = if (hasReadSms()) SmsHistory.recentInboundBodies(this)
                        else null
        if (NotifRules.isSmsEcho(pkg, text!!, smsBodies)) return

        val key = "$pkg|$title|$text"
        val now = System.currentTimeMillis()
        synchronized(seen) {
            seen.removeAll { now - it.second > 30_000 }
            if (seen.any { it.first == key }) return
            seen.addLast(key to now)
            while (seen.size > 40) seen.removeFirst()
        }

        UploadQueue.enqueue(this, Msg(
            sender = title!!,
            text = text,
            whenMs = sbn.postTime,
            channel = NotifRules.channelFor(pkg),
            direction = "in",
            source = "live"))
    }

    private fun hasReadSms(): Boolean =
        checkSelfPermission(android.Manifest.permission.READ_SMS) ==
            PackageManager.PERMISSION_GRANTED
}
