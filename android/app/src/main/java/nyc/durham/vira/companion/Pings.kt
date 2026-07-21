package nyc.durham.vira.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.app.PendingIntent
import org.json.JSONObject

/** Vira -> phone: one long-poll loop against /api/companion/pings. The
 *  hub holds the request open (~25s) and answers the moment something
 *  lands, so delivery is near-instant with no push service. Runs inside
 *  whatever process is alive (the notification listener normally); safe
 *  to call ensureRunning from anywhere. */
object Pings {
    const val CHANNEL_ID = "vira-pings"
    private var thread: Thread? = null
    private val lock = Object()

    fun ensureRunning(ctx: Context) {
        val app = ctx.applicationContext
        synchronized(lock) {
            if (thread?.isAlive == true) return
            thread = Thread({ loop(app) }, "vira-pings").apply {
                isDaemon = true
                start()
            }
        }
    }

    private fun loop(ctx: Context) {
        val store = Store(ctx)
        var backoffS = 15L
        while (true) {
            if (!store.paired) {
                Thread.sleep(30_000)
                continue
            }
            try {
                val res = HubClient(store).pings(store.pingAfter, 25)
                val arr = res.optJSONArray("pings")
                val got = mutableListOf<JSONObject>()
                if (arr != null) for (i in 0 until arr.length())
                    got.add(arr.getJSONObject(i))
                if (got.isNotEmpty()) {
                    store.addPings(got)
                    store.pingAfter = got.maxOf { it.optLong("id") }
                    got.forEach { show(ctx, it.optString("text")) }
                }
                backoffS = 15L
            } catch (_: Exception) {
                Thread.sleep(backoffS * 1000)
                backoffS = (backoffS * 2).coerceAtMost(120L)
            }
        }
    }

    fun ensureChannel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_ID, "Vira pings", NotificationManager.IMPORTANCE_DEFAULT
        ).apply { description = "Messages from your Vira hub" })
    }

    private fun show(ctx: Context, text: String) {
        if (text.isBlank()) return
        ensureChannel(ctx)
        val nm = ctx.getSystemService(NotificationManager::class.java)
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_vira)
            .setContentTitle("Vira")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        nm.notify(text.hashCode(), n)
    }
}
