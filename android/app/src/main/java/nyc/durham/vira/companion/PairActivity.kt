package nyc.durham.vira.companion

import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Scan the hub's pairing QR (or paste the pairing text) and claim it.
 *  Camera denied? The paste path is fully equivalent — the QR is just
 *  the same JSON. */
class PairActivity : ComponentActivity() {

    private val reader = MultiFormatReader().apply {
        setHints(mapOf(DecodeHintType.POSSIBLE_FORMATS to
                       listOf(BarcodeFormat.QR_CODE)))
    }
    private val handled = AtomicBoolean(false)
    private val camExecutor = Executors.newSingleThreadExecutor()
    private var preview: PreviewView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = Ui.column(this)
        root.addView(Ui.title(this, "Pair with your hub").apply {
            textSize = 20f
        })
        root.addView(Ui.body(this,
            "Point the camera at the QR in Vira's Phone Link window."))

        preview = PreviewView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, Ui.dp(context, 300)
            ).apply { topMargin = Ui.dp(context, 10) }
        }
        root.addView(preview)

        root.addView(Ui.body(this, "Can't scan? Paste the pairing text:")
            .apply { setPadding(0, Ui.dp(this@PairActivity, 14), 0, 0) })
        val paste = EditText(this).apply {
            hint = "{\"kind\":\"vira-pair\", …}"
            setHintTextColor(Ui.DIM)
            setTextColor(Ui.TEXT)
            setBackgroundColor(Ui.CARD)
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 2
            setPadding(Ui.dp(context, 10), Ui.dp(context, 10),
                       Ui.dp(context, 10), Ui.dp(context, 10))
        }
        root.addView(paste)
        root.addView(Ui.button(this, "Pair from pasted text",
                               primary = true) {
            val p = PairPayload.parse(paste.text.toString())
            if (p == null)
                Toast.makeText(this, "That doesn't look like a Vira " +
                    "pairing code", Toast.LENGTH_LONG).show()
            else claim(p)
        })

        setContentView(ScrollView(this).apply {
            setBackgroundColor(Ui.BG)
            addView(root)
        })

        if (checkSelfPermission(android.Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED) startCamera()
        else requestPermissions(
            arrayOf(android.Manifest.permission.CAMERA), 10)
    }

    override fun onRequestPermissionsResult(
        code: Int, perms: Array<String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        if (code == 10 && results.firstOrNull()
                == PackageManager.PERMISSION_GRANTED) startCamera()
        else Toast.makeText(this,
            "No camera — paste the pairing text instead",
            Toast.LENGTH_LONG).show()
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val prev = Preview.Builder().build().also {
                it.setSurfaceProvider(preview!!.surfaceProvider)
            }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(
                    ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build().also { a ->
                    a.setAnalyzer(camExecutor) { img -> scanFrame(img) }
                }
            provider.unbindAll()
            provider.bindToLifecycle(this,
                CameraSelector.DEFAULT_BACK_CAMERA, prev, analysis)
        }, mainExecutor)
    }

    private fun scanFrame(image: ImageProxy) {
        try {
            if (handled.get()) return
            val plane = image.planes[0]
            val buf = plane.buffer
            val data = ByteArray(buf.remaining())
            buf.get(data)
            val stride = plane.rowStride
            val rows = data.size / stride
            if (rows < 1) return
            val src = PlanarYUVLuminanceSource(
                data, stride, rows, 0, 0,
                minOf(image.width, stride), minOf(image.height, rows), false)
            val text = try {
                reader.decodeWithState(
                    BinaryBitmap(HybridBinarizer(src))).text
            } catch (_: Exception) { null } finally { reader.reset() }
            val p = PairPayload.parse(text)
            if (p != null && handled.compareAndSet(false, true))
                runOnUiThread { claim(p) }
        } finally {
            image.close()
        }
    }

    private fun claim(p: PairPayload) {
        Toast.makeText(this, "Pairing…", Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val name = (Build.MANUFACTURER + " " + Build.MODEL).trim()
                HubClient(Store(this)).pair(
                    p, name, "android " + Build.VERSION.RELEASE)
                val store = Store(this)
                store.hubUrl = p.url
                store.deviceId = p.deviceId
                store.token = p.token
                runOnUiThread {
                    Toast.makeText(this, "Paired with the hub",
                                   Toast.LENGTH_LONG).show()
                    Pings.ensureRunning(this)
                    finish()
                }
            } catch (e: Exception) {
                handled.set(false)
                runOnUiThread {
                    Toast.makeText(this, "Pairing failed: ${e.message}",
                                   Toast.LENGTH_LONG).show()
                }
            }
        }.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        camExecutor.shutdown()
    }
}
