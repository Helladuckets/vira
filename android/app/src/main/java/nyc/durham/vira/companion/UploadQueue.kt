package nyc.durham.vira.companion

import android.content.Context
import java.io.File
import org.json.JSONObject

/** The one path every captured message takes to the hub: append to the
 *  durable queue, and a single drain thread ships batches with backoff.
 *  Callers (SmsReceiver, NotificationCapture, the history backfill's
 *  live tail) never talk to the network themselves. */
object UploadQueue {
    private const val BATCH = 100
    private val lock = Object()
    private var queue: QueueFile? = null
    private var store: Store? = null
    private var thread: Thread? = null

    @Volatile var lastError: String = ""
        private set

    fun init(ctx: Context) {
        synchronized(lock) {
            if (queue == null) {
                queue = QueueFile(File(ctx.filesDir, "upload-queue.jsonl"))
                store = Store(ctx.applicationContext)
            }
        }
        ensureDrain()
    }

    fun enqueue(ctx: Context, msg: Msg) {
        init(ctx)
        synchronized(lock) {
            queue!!.append(msg.toJson().toString())
            (lock as Object).notifyAll()
        }
        ensureDrain()
    }

    fun pending(): Int = synchronized(lock) { queue?.size() ?: 0 }

    private fun ensureDrain() {
        synchronized(lock) {
            if (thread?.isAlive == true) return
            thread = Thread({ drainLoop() }, "vira-upload").apply {
                isDaemon = true
                start()
            }
        }
    }

    private fun drainLoop() {
        var backoffS = 15L
        while (true) {
            val (st, q) = synchronized(lock) { store to queue }
            if (st == null || q == null) return
            if (!st.paired || q.size() == 0) {
                synchronized(lock) { (lock as Object).wait(30_000) }
                continue
            }
            val lines = synchronized(lock) { q.peek(BATCH) }
            val msgs = lines.mapNotNull {
                try { Msg.fromJson(JSONObject(it)) } catch (_: Exception) { null }
            }
            try {
                if (msgs.isNotEmpty()) {
                    val res = HubClient(st).push(msgs)
                    st.uploadedTotal += res.optInt("new", 0)
                }
                synchronized(lock) { q.drop(lines.size) }
                lastError = ""
                backoffS = 15L
            } catch (e: Exception) {
                lastError = e.message ?: "upload failed"
                synchronized(lock) { (lock as Object).wait(backoffS * 1000) }
                backoffS = (backoffS * 2).coerceAtMost(300L)
            }
        }
    }
}
