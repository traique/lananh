package vn.lananh.manhinh

import android.graphics.Color
import java.text.DecimalFormat
import java.text.DecimalFormatSymbols
import java.util.Locale

object Colors {
    val BG = Color.parseColor("#000000")
    val PANEL = Color.parseColor("#101214")
    val TEXT = Color.parseColor("#ECEDEE")
    val DIM = Color.parseColor("#8E949A")
    val ACCENT = Color.parseColor("#FFC857")
    val ON_ACCENT = Color.parseColor("#1A1300")
    val UP = Color.parseColor("#3DDC84")
    val DOWN = Color.parseColor("#FF5A5F")
    val REF = Color.parseColor("#FFD54F")
    val SUNDAY = Color.parseColor("#FF7A7A")
    val HOLIDAY = Color.parseColor("#FF9F43")
    val GOOD_DAY = Color.parseColor("#7FD1FF")
    val WARN = Color.parseColor("#FFB74D")
}

/** Định dạng số kiểu Việt Nam: 1.234.567 và 12,5. */
object Fmt {
    private val vi = DecimalFormatSymbols(Locale("vi", "VN")).apply {
        groupingSeparator = '.'
        decimalSeparator = ','
    }

    fun int(value: Double): String = DecimalFormat("#,##0", vi).format(value)

    fun dec(value: Double, digits: Int = 1): String {
        val pattern = if (digits <= 0) "#,##0" else "#,##0." + "0".repeat(digits)
        return DecimalFormat(pattern, vi).format(value)
    }

    fun signed(value: Double, digits: Int = 2): String = (if (value > 0) "+" else "") + dec(value, digits)

    /** 120500000 -> "120,5" (triệu). */
    fun million(value: Double): String = dec(value / 1_000_000.0, 1)

    fun changeColor(value: Double): Int = when {
        value > 0 -> Colors.UP
        value < 0 -> Colors.DOWN
        else -> Colors.REF
    }

    fun arrow(value: Double): String = when {
        value > 0 -> "▲"
        value < 0 -> "▼"
        else -> "■"
    }
}

/** Mã thời tiết WMO (Open-Meteo) -> biểu tượng. */
object WeatherIcon {
    fun of(code: Int?, description: String? = null): String {
        if (code == null) return byText(description.orEmpty())
        return when (code) {
            0 -> "☀️"
            1 -> "🌤️"
            2 -> "⛅"
            3 -> "☁️"
            45, 48 -> "🌫️"
            in 51..57 -> "🌦️"
            in 61..67 -> "🌧️"
            in 71..77, 85, 86 -> "❄️"
            in 80..82 -> "🌦️"
            in 95..99 -> "⛈️"
            else -> byText(description.orEmpty())
        }
    }

    private fun byText(text: String): String {
        val t = text.lowercase(Locale("vi"))
        return when {
            "dông" in t || "sấm" in t -> "⛈️"
            "mưa" in t -> "🌧️"
            "sương" in t -> "🌫️"
            "nắng" in t || "quang" in t -> "☀️"
            "mây" in t -> "⛅"
            else -> "🌡️"
        }
    }
}
