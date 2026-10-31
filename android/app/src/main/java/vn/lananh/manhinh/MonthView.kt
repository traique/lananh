package vn.lananh.manhinh

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.view.View
import org.json.JSONObject
import kotlin.math.ceil
import kotlin.math.min

/**
 * Lịch tháng dương + âm vẽ thẳng lên Canvas (nhẹ hơn 42 TextView trên máy yếu).
 * Dữ liệu: ``calendar.month`` của API (days[]: day, lunarDay, lunarMonth, weekday
 * 0 = Thứ Hai, isHoangDao, holidays[]).
 */
class MonthView(context: Context) : View(context) {
    var month: JSONObject? = null
        set(value) {
            field = value
            invalidate()
        }
    var today: String = ""
        set(value) {
            field = value
            invalidate()
        }

    private val headers = arrayOf("T2", "T3", "T4", "T5", "T6", "T7", "CN")
    private val solarPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textAlign = Paint.Align.CENTER
        typeface = Typeface.DEFAULT_BOLD
    }
    private val lunarPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { textAlign = Paint.Align.CENTER }
    private val headerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textAlign = Paint.Align.CENTER
        color = Colors.DIM
    }
    private val todayPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Colors.ACCENT }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Colors.GOOD_DAY }
    private val rect = RectF()

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val days = month?.optJSONArray("days") ?: return
        if (days.length() == 0) return
        val w = width.toFloat()
        val h = height.toFloat()
        val first = days.getJSONObject(0).optInt("weekday")
        val rows = ceil((first + days.length()) / 7.0).toInt()
        val headerH = min(h * 0.12f, 28f * resources.displayMetrics.density)
        val cellW = w / 7
        val cellH = (h - headerH) / rows

        headerPaint.textSize = headerH * 0.6f
        for (i in 0 until 7) {
            headerPaint.color = if (i == 6) Colors.SUNDAY else Colors.DIM
            canvas.drawText(headers[i], cellW * (i + 0.5f), headerH * 0.72f, headerPaint)
        }

        val solarSize = min(cellH * 0.46f, cellW * 0.42f)
        val lunarSize = solarSize * 0.55f
        for (i in 0 until days.length()) {
            val d = days.getJSONObject(i)
            val index = first + i
            val col = index % 7
            val row = index / 7
            val left = col * cellW
            val top = headerH + row * cellH
            val isToday = d.optString("date") == today
            val holiday = (d.optJSONArray("holidays")?.length() ?: 0) > 0
            if (isToday) {
                rect.set(left + 2, top + 2, left + cellW - 2, top + cellH - 2)
                canvas.drawRoundRect(rect, 8f, 8f, todayPaint)
            }
            solarPaint.textSize = solarSize
            solarPaint.color = when {
                isToday -> Colors.ON_ACCENT
                holiday -> Colors.HOLIDAY
                col == 6 -> Colors.SUNDAY
                else -> Colors.TEXT
            }
            val cx = left + cellW / 2
            canvas.drawText(d.optInt("day").toString(), cx, top + cellH * 0.52f, solarPaint)

            val lunarDay = d.optInt("lunarDay")
            val lunarText = if (lunarDay == 1) "$lunarDay/${d.optInt("lunarMonth")}" else lunarDay.toString()
            lunarPaint.textSize = lunarSize
            lunarPaint.color = when {
                isToday -> Colors.ON_ACCENT
                lunarDay == 1 || lunarDay == 15 -> Colors.ACCENT
                else -> Colors.DIM
            }
            canvas.drawText(lunarText, cx, top + cellH * 0.86f, lunarPaint)
            if (d.optBoolean("isHoangDao") && !isToday) {
                canvas.drawCircle(left + cellW * 0.16f, top + cellH * 0.18f, cellH * 0.05f, dotPaint)
            }
        }
    }
}
