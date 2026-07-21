package nyc.durham.vira.companion

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast

/** The whole app on one screen: pairing state, plain-language permission
 *  cards (this is someone else's phone — the consent copy IS the
 *  product), history backfill, and recent pings. Everything re-renders
 *  from real state on every resume, so granting a permission in Settings
 *  and coming back just works. */
class MainActivity : Activity() {

    private lateinit var store: Store
    private lateinit var root: LinearLayout
    private val ui = Handler(Looper.getMainLooper())
    private var backfillRunning = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this)
        UploadQueue.init(this)
        root = Ui.column(this)
        val scroll = ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        }
        setContentView(scroll)
    }

    override fun onResume() {
        super.onResume()
        render()
        if (store.paired) Pings.ensureRunning(this)
    }

    // ---------- state checks ----------

    private fun granted(perm: String) =
        checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED

    private fun smsGranted() = granted(android.Manifest.permission.READ_SMS)

    private fun notifAccessGranted(): Boolean {
        val flat = Settings.Secure.getString(
            contentResolver, "enabled_notification_listeners") ?: return false
        return flat.contains(packageName)
    }

    private fun postNotifGranted(): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            granted(android.Manifest.permission.POST_NOTIFICATIONS)

    // ---------- rendering ----------

    private fun render() {
        root.removeAllViews()
        header()
        if (!store.paired) {
            pairCard()
            privacyCard()
            return
        }
        hubCard()
        permissionCards()
        historyCard()
        pingsCard()
        privacyCard()
    }

    private fun header() {
        root.addView(Ui.title(this, "Vira Companion").apply { textSize = 22f })
        root.addView(Ui.body(this,
            "This phone's line to your Vira hub — texts and message " +
            "notifications go to YOUR computer, nowhere else.").apply {
            setPadding(0, Ui.dp(this@MainActivity, 2), 0,
                       Ui.dp(this@MainActivity, 12))
        })
    }

    private fun pairCard() {
        val c = Ui.card(this)
        c.addView(Ui.title(this, "Not paired yet"))
        c.addView(Ui.body(this,
            "On the hub computer, open Vira and press “Pair a " +
            "phone” in the Phone Link window. Then scan the code " +
            "here — or paste the pairing text if scanning is awkward. " +
            "The phone and the hub need to see each other: same " +
            "Tailscale network (or the same Wi‑Fi)."))
        c.addView(Ui.button(this, "Scan or paste the pairing code",
                            primary = true) {
            startActivity(Intent(this, PairActivity::class.java))
        })
        root.addView(c)
    }

    private fun hubCard() {
        val c = Ui.card(this)
        c.addView(Ui.title(this, "Paired"))
        c.addView(Ui.body(this, "Hub: " + store.hubUrl))
        c.addView(Ui.status(this, "connected as " + store.deviceId, Ui.GREEN))
        c.addView(Ui.button(this, "Unpair this phone") {
            store.unpair()
            Toast.makeText(this,
                "Unpaired. Also remove the device in the hub's Phone " +
                "Link window.", Toast.LENGTH_LONG).show()
            render()
        })
        root.addView(c)
    }

    private fun permissionCards() {
        // SMS — history + live capture
        val sms = Ui.card(this)
        sms.addView(Ui.title(this, "Text messages"))
        sms.addView(Ui.body(this,
            "Lets Vira read the SMS conversations on this phone and see " +
            "new ones as they arrive. They are stored only on the hub " +
            "computer you paired with — no company server, no cloud. " +
            "Vira uses them to remember who said what."))
        if (smsGranted() && granted(android.Manifest.permission.RECEIVE_SMS)) {
            sms.addView(Ui.status(this, "on", Ui.GREEN))
        } else {
            sms.addView(Ui.status(this, "off — texts are not being shared",
                                  Ui.RED))
            sms.addView(Ui.button(this, "Allow reading texts",
                                  primary = true) {
                requestPermissions(arrayOf(
                    android.Manifest.permission.READ_SMS,
                    android.Manifest.permission.RECEIVE_SMS), 1)
            })
        }
        root.addView(sms)

        // Notification access — RCS + WhatsApp
        val na = Ui.card(this)
        na.addView(Ui.title(this, "Message notifications"))
        na.addView(Ui.body(this,
            "RCS chats and WhatsApp keep their messages locked away, so " +
            "the only way Vira can see an incoming one is through its " +
            "notification. Vira only records notifications from " +
            "messaging apps (Messages, WhatsApp) — nothing from any " +
            "other app is captured or sent anywhere."))
        if (notifAccessGranted()) {
            na.addView(Ui.status(this, "on", Ui.GREEN))
        } else {
            na.addView(Ui.status(this,
                "off — RCS and WhatsApp messages are invisible to Vira",
                Ui.RED))
            na.addView(Ui.button(this,
                "Turn on in settings (pick Vira Companion)",
                primary = true) {
                startActivity(
                    Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS"))
            })
        }
        root.addView(na)

        // Post notifications — pings
        if (!postNotifGranted()) {
            val pn = Ui.card(this)
            pn.addView(Ui.title(this, "Vira's pings"))
            pn.addView(Ui.body(this,
                "Let the app show a notification when Vira has " +
                "something for you — that is the only thing it posts."))
            pn.addView(Ui.button(this, "Allow notifications",
                                 primary = true) {
                requestPermissions(arrayOf(
                    android.Manifest.permission.POST_NOTIFICATIONS), 2)
            })
            root.addView(pn)
        }
    }

    private fun historyCard() {
        val c = Ui.card(this)
        c.addView(Ui.title(this, "Message history"))
        val pending = UploadQueue.pending()
        val done = store.smsWatermark > 0
        val sub = StringBuilder()
        sub.append(if (done) "History uploaded through " +
                java.text.DateFormat.getDateTimeInstance(
                    java.text.DateFormat.MEDIUM, java.text.DateFormat.SHORT)
                    .format(java.util.Date(store.smsWatermark))
            else "Nothing uploaded yet.")
        sub.append("  Sent so far: ${store.uploadedTotal}.")
        if (pending > 0) sub.append("  Waiting to send: $pending.")
        if (UploadQueue.lastError.isNotEmpty())
            sub.append("  Last problem: ${UploadQueue.lastError}")
        c.addView(Ui.body(this, sub.toString()))
        if (smsGranted() && !backfillRunning) {
            c.addView(Ui.button(this,
                if (done) "Upload newer messages" else "Upload SMS history",
                primary = !done) { runBackfill(c) })
        } else if (backfillRunning) {
            c.addView(Ui.status(this, "uploading…", Ui.GOLD))
        }
        root.addView(c)
    }

    private fun runBackfill(card: LinearLayout) {
        backfillRunning = true
        render()
        Thread {
            try {
                val n = SmsHistory.backfill(this, store) { done, total ->
                    ui.post {
                        Toast.makeText(this,
                            "Queued $done of $total", Toast.LENGTH_SHORT).show()
                    }
                }
                ui.post {
                    backfillRunning = false
                    Toast.makeText(this,
                        if (n > 0) "Queued $n messages — they upload in " +
                            "the background" else "Nothing new to upload",
                        Toast.LENGTH_LONG).show()
                    render()
                }
            } catch (e: Exception) {
                ui.post {
                    backfillRunning = false
                    Toast.makeText(this, "History read failed: ${e.message}",
                                   Toast.LENGTH_LONG).show()
                    render()
                }
            }
        }.start()
    }

    private fun pingsCard() {
        val c = Ui.card(this)
        c.addView(Ui.title(this, "Pings from Vira"))
        val recent = store.recentPings()
        if (recent.isEmpty()) {
            c.addView(Ui.body(this,
                "When Vira wants your attention — an email worth seeing, " +
                "a renewal, a finished job — it lands here as a normal " +
                "notification."))
        } else {
            recent.takeLast(5).reversed().forEach { (created, text) ->
                c.addView(Ui.body(this, "• $text").apply {
                    setTextColor(Ui.TEXT)
                })
                c.addView(Ui.body(this, created).apply { textSize = 11f })
            }
        }
        c.addView(Ui.button(this, "Check now") {
            Thread {
                try {
                    val res = HubClient(store).pings(store.pingAfter, 0)
                    val n = res.optJSONArray("pings")?.length() ?: 0
                    ui.post {
                        Toast.makeText(this,
                            if (n > 0) "$n new" else "Nothing new — connected",
                            Toast.LENGTH_SHORT).show()
                        render()
                    }
                } catch (e: Exception) {
                    ui.post { Toast.makeText(this,
                        "Hub unreachable: ${e.message}",
                        Toast.LENGTH_LONG).show() }
                }
            }.start()
        })
        root.addView(c)
    }

    private fun privacyCard() {
        val c = Ui.card(this)
        c.addView(Ui.title(this, "Where things go"))
        c.addView(Ui.body(this,
            "Everything this app captures goes to one place: the Vira " +
            "hub you paired with, over your own network. Nothing is " +
            "sent to Vira's makers or any third party. Uninstalling " +
            "the app (or unpairing) stops all of it."))
        root.addView(c)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>,
        grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        render()
    }
}
