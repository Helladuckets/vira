package nyc.durham.vira.companion

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/** Tiny programmatic-view helpers: the app is a stack of cards, and
 *  building them in code keeps the whole UI in one language with no
 *  inflation plumbing. Colors mirror res/values/colors.xml. */
object Ui {
    const val BG = 0xFF101216.toInt()
    const val CARD = 0xFF1A1E26.toInt()
    const val LINE = 0xFF2A3040.toInt()
    const val TEXT = 0xFFE8E6DF.toInt()
    const val DIM = 0xFF9AA0AD.toInt()
    const val GOLD = 0xFFD4A843.toInt()
    const val GREEN = 0xFF5FB573.toInt()
    const val RED = 0xFFD06A5F.toInt()

    fun dp(ctx: Context, v: Int): Int =
        (v * ctx.resources.displayMetrics.density).toInt()

    fun column(ctx: Context): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(ctx, 16), dp(ctx, 16), dp(ctx, 16), dp(ctx, 24))
    }

    fun card(ctx: Context): LinearLayout = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        background = GradientDrawable().apply {
            setColor(CARD)
            cornerRadius = dp(ctx, 12).toFloat()
            setStroke(dp(ctx, 1), LINE)
        }
        setPadding(dp(ctx, 14), dp(ctx, 12), dp(ctx, 14), dp(ctx, 13))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = dp(ctx, 10) }
    }

    fun title(ctx: Context, s: String): TextView = TextView(ctx).apply {
        text = s
        setTextColor(TEXT)
        textSize = 16f
        typeface = Typeface.DEFAULT_BOLD
    }

    fun body(ctx: Context, s: String): TextView = TextView(ctx).apply {
        text = s
        setTextColor(DIM)
        textSize = 13.5f
        setPadding(0, dp(ctx, 4), 0, 0)
        setLineSpacing(0f, 1.15f)
    }

    fun status(ctx: Context, s: String, color: Int): TextView =
        TextView(ctx).apply {
            text = s
            setTextColor(color)
            textSize = 12.5f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(ctx, 4), 0, 0)
        }

    fun button(ctx: Context, s: String, primary: Boolean = false,
               onClick: (View) -> Unit): Button = Button(ctx).apply {
        text = s
        isAllCaps = false
        textSize = 14f
        setTextColor(if (primary) BG else TEXT)
        background = GradientDrawable().apply {
            setColor(if (primary) GOLD else CARD)
            cornerRadius = dp(ctx, 9).toFloat()
            if (!primary) setStroke(dp(ctx, 1), LINE)
        }
        setPadding(dp(ctx, 14), dp(ctx, 9), dp(ctx, 14), dp(ctx, 9))
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { topMargin = dp(ctx, 8) }
        setOnClickListener(onClick)
    }
}
