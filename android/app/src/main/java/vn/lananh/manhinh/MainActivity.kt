package vn.lananh.manhinh

import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.TextUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import kotlin.math.ceil
import kotlin.math.roundToInt

/**
 * Màn hình thông tin: trái cố định (giờ, ngày dương/âm, thời tiết), phải tự đổi trang
 * (lịch tháng, chứng khoán, vàng/xăng/tỷ giá, tin, dự báo). Chạm nửa phải để sang
 * trang; giữ lâu vào đồng hồ để mở Cài đặt.
 *
 * Màn hình: giờ ngày luôn sáng; giờ đêm giảm độ sáng và sau ``idleMinutes`` không
 * chạm / không vẫy tay thì bỏ cờ giữ sáng để hệ thống tự tắt màn hình (theo "Thời gian
 * chờ màn hình" của máy). WakeService bật lại khi có tay lại gần cảm biến.
 *
 * Dữ liệu: 1 request mỗi ``refreshSec`` (server trả 300) CHỈ khi màn hình đang hiện;
 * mở lại màn hình mà dữ liệu đã cũ thì lấy ngay. Lỗi thì thử lại sau 1 phút.
 */
class MainActivity : Activity() {
    companion object {
        const val EXTRA_WAKE = "wake"
        private const val TOUCH_PAUSE_MS = 60_000L
        private const val RETRY_MS = 60_000L
        private const val STALE_WARN_MS = 15 * 60_000L
        private const val POLICY_TICK_MS = 15_000L
        private const val NEWS_PER_PAGE = 4
        private val WEEKDAYS =
            arrayOf("Chủ Nhật", "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy")
        private val PAGES = arrayOf("Lịch tháng", "Chứng khoán", "Vàng · Xăng · Tỷ giá", "Tin tức", "Dự báo")
        private val SECTION_NAMES = mapOf(
            "weather" to "thời tiết",
            "gold" to "vàng",
            "fuel" to "xăng",
            "news" to "tin",
            "watchlist" to "chứng khoán",
        )
    }

    private lateinit var prefs: Prefs
    private val timers = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var data: JSONObject? = null
    private var lastOkAt = 0L
    private var errorText: String? = null
    private var loading = false
    private var resumed = false
    private var lastInteraction = SystemClock.elapsedRealtime()
    private var lastTouch = 0L
    private var page = 0
    private var newsOffset = 0

    private lateinit var clock: TextView
    private lateinit var dateLine: TextView
    private lateinit var lunarLine: TextView
    private lateinit var canChiLine: TextView
    private lateinit var extraLine: TextView
    private lateinit var weatherNow: TextView
    private lateinit var weatherDetail: TextView
    private lateinit var hoursRow: LinearLayout
    private lateinit var status: TextView
    private lateinit var pageTitle: TextView
    private lateinit var dots: TextView
    private lateinit var pageBox: FrameLayout

    // ─── Vòng đời ────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        applyLockScreenFlags()
        setContentView(buildLayout())
        hideSystemBars()
        data = Store.load(this)
        lastOkAt = data?.optLong("_fetchedAt") ?: 0L
        WakeService.sync(this)
        handleIntent(intent)
        if (savedInstanceState == null && !prefs.paired) openSettings() // lần đầu mở app
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        WakeBus.listener = { touch() }
        touch() // màn hình vừa sáng (người bật hoặc cảm biến): đếm lại thời gian chờ tắt
        if (prefs.forceRefresh) {
            // Vừa ghép lại / đổi máy chủ / đổi địa điểm trong Cài đặt: bỏ dữ liệu cũ.
            prefs.forceRefresh = false
            data = Store.load(this)
            lastOkAt = 0L
            errorText = null
        }
        render()
        timers.post(clockTick)
        timers.post(policyTick)
        timers.postDelayed(rotateTick, prefs.rotateSeconds * 1000L)
        val age = System.currentTimeMillis() - lastOkAt
        if (age >= refreshMs() || age < 0) refresh() else scheduleRefresh(refreshMs() - age)
    }

    override fun onPause() {
        resumed = false
        WakeBus.listener = null
        timers.removeCallbacksAndMessages(null)
        super.onPause()
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemBars()
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) {
            lastTouch = SystemClock.elapsedRealtime()
            touch()
        }
        return super.dispatchTouchEvent(ev)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_WAKE, false) == true) touch()
    }

    private fun openSettings() {
        startActivity(Intent(this, SettingsActivity::class.java))
    }

    // ─── Màn hình sáng/tắt ───────────────────────────────────────────────────

    @Suppress("DEPRECATION")
    private fun applyLockScreenFlags() {
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        window.addFlags(
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD or
                WindowManager.LayoutParams.FLAG_FULLSCREEN,
        )
    }

    @Suppress("DEPRECATION")
    private fun hideSystemBars() {
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN or
                View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
                View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or
                View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            )
    }

    private fun touch() {
        lastInteraction = SystemClock.elapsedRealtime()
        applyScreenPolicy()
    }

    private fun applyScreenPolicy() {
        val day = prefs.alwaysOn || prefs.isDaytime()
        val idle = SystemClock.elapsedRealtime() - lastInteraction
        val keep = day || idle < prefs.idleMinutes * 60_000L
        if (keep) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
        val target = if (day) {
            WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        } else {
            prefs.nightBrightness / 100f
        }
        val lp = window.attributes
        if (lp.screenBrightness != target) {
            lp.screenBrightness = target
            window.attributes = lp
        }
    }

    private val policyTick = object : Runnable {
        override fun run() {
            applyScreenPolicy()
            timers.postDelayed(this, POLICY_TICK_MS)
        }
    }

    private val clockTick = object : Runnable {
        override fun run() {
            renderClock()
            val now = System.currentTimeMillis()
            timers.postDelayed(this, 60_000L - now % 60_000L + 50L)
        }
    }

    private val rotateTick = object : Runnable {
        override fun run() {
            if (SystemClock.elapsedRealtime() - lastTouch > TOUCH_PAUSE_MS) nextPage()
            timers.postDelayed(this, prefs.rotateSeconds * 1000L)
        }
    }

    // ─── Lấy dữ liệu ─────────────────────────────────────────────────────────

    private fun refreshMs(): Long = (data?.optInt("refreshSec", 300) ?: 300).coerceIn(60, 3600) * 1000L

    private val refreshTick = Runnable { refresh() }

    private fun scheduleRefresh(delayMs: Long) {
        timers.removeCallbacks(refreshTick)
        timers.postDelayed(refreshTick, delayMs.coerceAtLeast(1_000L))
    }

    private fun refresh() {
        if (!prefs.paired) {
            errorText = "Chưa ghép với bot - chạm vào đây để mở Cài đặt."
            renderStatus()
            return
        }
        if (loading) return
        loading = true
        val ctx = applicationContext
        val server = prefs.serverUrl
        val token = prefs.token
        val place = prefs.place
        io.execute {
            val result = try {
                Result.success(Api.dashboard(ctx, server, token, place))
            } catch (e: Exception) {
                Result.failure(e)
            }
            runOnUiThread { onRefreshed(result) }
        }
    }

    private fun onRefreshed(result: Result<JSONObject>) {
        loading = false
        if (isDestroyed) return
        result.onSuccess { fresh ->
            val merged = Store.merge(data, fresh)
            lastOkAt = System.currentTimeMillis()
            merged.put("_fetchedAt", lastOkAt)
            data = merged
            errorText = null
            val text = merged.toString()
            val ctx = applicationContext
            io.execute { Store.saveText(ctx, text) }
            render()
            if (resumed) scheduleRefresh(refreshMs())
        }.onFailure { e ->
            errorText = when {
                e is ApiException && e.status == 401 -> {
                    prefs.token = ""
                    "Thiết bị đã bị thu hồi - ghép lại trong Cài đặt."
                }
                e is ApiException -> e.message
                else -> "Mất kết nối, đang thử lại"
            }
            renderStatus()
            if (resumed && prefs.paired) scheduleRefresh(RETRY_MS)
        }
    }

    // ─── Bố cục ──────────────────────────────────────────────────────────────

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).roundToInt()

    private fun text(sizeSp: Float, textColor: Int = Colors.TEXT, bold: Boolean = false, maxLineCount: Int = 1): TextView =
        TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
            setTextColor(textColor)
            if (bold) setTypeface(Typeface.DEFAULT, Typeface.BOLD)
            includeFontPadding = false
            maxLines = maxLineCount
            ellipsize = TextUtils.TruncateAt.END
        }

    private fun vertical(): LinearLayout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

    private fun horizontal(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    private fun LinearLayout.add(view: View, topDp: Int = 0, weight: Float = 0f): View {
        val lp = if (orientation == LinearLayout.VERTICAL) {
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                if (weight > 0) 0 else LinearLayout.LayoutParams.WRAP_CONTENT,
                weight,
            )
        } else {
            LinearLayout.LayoutParams(
                if (weight > 0) 0 else LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                weight,
            )
        }
        lp.topMargin = dp(topDp)
        addView(view, lp)
        return view
    }

    private fun buildLayout(): View {
        val root = horizontal().apply {
            gravity = Gravity.NO_GRAVITY
            setBackgroundColor(Colors.BG)
            setPadding(dp(14), dp(10), dp(10), dp(10))
        }

        val left = vertical().apply { setPadding(0, 0, dp(12), 0) }
        clock = text(68f, bold = true)
        clock.setOnLongClickListener {
            openSettings()
            true
        }
        dateLine = text(19f)
        lunarLine = text(17f, Colors.ACCENT)
        canChiLine = text(13f, Colors.DIM)
        extraLine = text(13f, Colors.HOLIDAY, maxLineCount = 2)
        weatherNow = text(26f)
        weatherDetail = text(13f, Colors.DIM)
        hoursRow = horizontal()
        status = text(12f, Colors.DIM, maxLineCount = 2)
        status.setOnClickListener { if (!prefs.paired) openSettings() }
        left.add(clock)
        left.add(dateLine, 4)
        left.add(lunarLine, 6)
        left.add(canChiLine, 4)
        left.add(extraLine, 4)
        left.add(weatherNow, 14)
        left.add(weatherDetail, 4)
        left.add(hoursRow, 10)
        left.add(View(this), weight = 1f)
        left.add(status)
        root.addView(left, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 1f))

        val right = vertical().apply {
            setBackgroundColor(Colors.PANEL)
            setPadding(dp(12), dp(8), dp(12), dp(8))
            setOnClickListener { nextPage() }
        }
        val header = horizontal()
        pageTitle = text(17f, Colors.ACCENT, bold = true)
        dots = text(11f, Colors.DIM)
        header.add(pageTitle, weight = 1f)
        header.add(dots)
        pageBox = FrameLayout(this)
        right.add(header)
        right.add(pageBox, 8, weight = 1f)
        root.addView(right, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 1.3f))
        return root
    }

    // ─── Vẽ dữ liệu ──────────────────────────────────────────────────────────

    private fun todayIso(): String = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date())

    private fun JSONObject.num(key: String): Double? =
        if (isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

    private fun section(key: String): JSONObject? = data?.optJSONObject(key)

    private fun render() {
        renderClock()
        renderLeft()
        renderPage()
        renderStatus()
    }

    private fun renderClock() {
        val now = Calendar.getInstance()
        clock.text = SimpleDateFormat("HH:mm", Locale.US).format(now.time)
        dateLine.text = WEEKDAYS[now.get(Calendar.DAY_OF_WEEK) - 1] + ", " +
            SimpleDateFormat("dd/MM/yyyy", Locale.US).format(now.time)
        val serverDay = data?.optJSONObject("calendar")?.optJSONObject("today")?.optString("date")
        if (data != null && serverDay != todayIso()) {
            renderLeft() // qua ngày mới: ẩn âm lịch cũ, lấy dữ liệu mới
            if (resumed && !loading) refresh()
        }
    }

    private fun renderLeft() {
        val today = data?.optJSONObject("calendar")?.optJSONObject("today")
        val lunar = today?.optJSONObject("lunar")
        if (today != null && lunar != null && today.optString("date") == todayIso()) {
            val leap = if (lunar.optBoolean("isLeapMonth")) " nhuận" else ""
            lunarLine.text = "Âm lịch ${lunar.optInt("day")}/${lunar.optInt("month")}$leap · năm ${lunar.optString("yearCanChi")}"
            val good = if (today.optBoolean("isHoangDao")) " · Hoàng đạo" else ""
            canChiLine.text = "Ngày ${lunar.optString("dayCanChi")} · tháng ${lunar.optString("monthCanChi")}$good"
            val extras = mutableListOf<String>()
            today.optJSONArray("holidays")?.let { arr -> for (i in 0 until arr.length()) extras += arr.optString(i) }
            today.optString("solarTerm").takeIf { it.isNotBlank() && it != "null" }?.let { extras += "Tiết $it" }
            val toTet = today.optInt("daysToTet", -1)
            if (toTet in 1..45) extras += "Còn $toTet ngày Tết"
            extraLine.text = extras.joinToString(" · ")
            extraLine.visibility = if (extras.isEmpty()) View.GONE else View.VISIBLE
        } else {
            lunarLine.text = ""
            canChiLine.text = ""
            extraLine.visibility = View.GONE
        }

        val w = section("weather")
        val now = w?.optJSONObject("now")
        hoursRow.removeAllViews()
        if (w == null || now == null) {
            weatherNow.text = if (w?.has("error") == true) "🌡️ --" else ""
            weatherDetail.text = w?.optString("error").orEmpty()
            return
        }
        val temp = now.num("temperatureC")?.roundToInt()?.toString() ?: "--"
        val code = if (now.isNull("code")) null else now.optInt("code")
        weatherNow.text = "${WeatherIcon.of(code, now.optString("description"))} $temp° ${now.optString("description")}"
        val detail = mutableListOf(w.optString("location"))
        now.num("feelsLikeC")?.let { detail += "cảm giác ${it.roundToInt()}°" }
        now.num("humidity")?.let { detail += "ẩm ${it.roundToInt()}%" }
        weatherDetail.text = detail.filter { it.isNotBlank() }.joinToString(" · ")

        val hours = w.optJSONArray("hours") ?: JSONArray()
        var i = 2
        while (i < hours.length() && hoursRow.childCount < 5) {
            val h = hours.getJSONObject(i)
            val cell = vertical().apply { gravity = Gravity.CENTER_HORIZONTAL }
            val hourText = h.optString("time").substringAfter('T').substringBefore(':') + "h"
            cell.add(text(12f, Colors.DIM).apply { this.text = hourText; gravity = Gravity.CENTER })
            val hc = if (h.isNull("code")) null else h.optInt("code")
            cell.add(text(16f).apply { this.text = WeatherIcon.of(hc); gravity = Gravity.CENTER }, 2)
            val ht = h.num("temperatureC")?.roundToInt()?.toString() ?: "--"
            cell.add(text(13f).apply { this.text = "$ht°"; gravity = Gravity.CENTER }, 2)
            val rain = h.num("rainChance")?.roundToInt() ?: 0
            cell.add(
                text(11f, if (rain >= 50) Colors.GOOD_DAY else Colors.DIM).apply {
                    this.text = "☂$rain%"
                    gravity = Gravity.CENTER
                },
                2,
            )
            hoursRow.add(cell, weight = 1f)
            i += 2
        }
    }

    private fun renderStatus() {
        val parts = mutableListOf<String>()
        if (lastOkAt > 0) {
            parts += "Cập nhật " + SimpleDateFormat("HH:mm", Locale.US).format(Date(lastOkAt))
        }
        val stale = SECTION_NAMES.filterKeys { section(it)?.optBoolean("stale") == true }.values
        if (stale.isNotEmpty()) parts += "dữ liệu cũ: " + stale.joinToString(", ")
        errorText?.let { parts += "⚠ $it" }
        status.text = parts.joinToString(" · ")
        val old = lastOkAt > 0 && System.currentTimeMillis() - lastOkAt > STALE_WARN_MS
        status.setTextColor(if (errorText != null || old || stale.isNotEmpty()) Colors.WARN else Colors.DIM)
    }

    private fun nextPage() {
        if (PAGES[page] == "Tin tức") newsOffset += NEWS_PER_PAGE
        page = (page + 1) % PAGES.size
        renderPage()
    }

    private fun renderPage() {
        pageTitle.text = PAGES[page]
        dots.text = PAGES.indices.joinToString(" ") { if (it == page) "●" else "○" }
        pageBox.removeAllViews()
        val view = if (data == null) {
            message(if (prefs.paired) "Đang tải dữ liệu…" else "Giữ lâu vào đồng hồ để mở Cài đặt và ghép với bot.")
        } else {
            when (page) {
                0 -> calendarPage()
                1 -> stocksPage()
                2 -> pricesPage()
                3 -> newsPage()
                else -> forecastPage()
            }
        }
        pageBox.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
    }

    private fun message(msg: String): View = text(16f, Colors.DIM, maxLineCount = 6).apply {
        text = msg
        gravity = Gravity.CENTER
    }

    private fun sectionError(s: JSONObject?): View? = when {
        s == null -> message("Chưa có dữ liệu.")
        s.has("error") -> message(s.optString("error"))
        else -> null
    }

    private fun calendarPage(): View {
        val month = data?.optJSONObject("calendar")?.optJSONObject("month") ?: return message("Chưa có lịch.")
        pageTitle.text = "Lịch tháng ${month.optInt("month")}/${month.optInt("year")}"
        val box = vertical()
        val grid = MonthView(this).apply {
            this.month = month
            today = todayIso()
        }
        box.add(grid, weight = 1f)
        box.add(text(11f, Colors.DIM).apply { text = "Số nhỏ: ngày âm · chấm xanh: ngày hoàng đạo" }, 4)
        return box
    }

    private fun stocksPage(): View {
        val s = section("watchlist")
        sectionError(s)?.let { return it }
        val items = s!!.optJSONArray("items") ?: JSONArray()
        if (items.length() == 0) return message("Chưa có mã theo dõi.\nNhắn bot: /theodoi FPT HPG")
        val box = vertical()
        box.add(
            text(12f, Colors.DIM).apply {
                text = if (s.optBoolean("marketOpen")) "Đang trong phiên" else "Ngoài giờ giao dịch · giá phiên gần nhất"
            },
        )
        val perColumn = 8
        val columns = ceil(items.length() / perColumn.toDouble()).toInt().coerceIn(1, 3)
        val size = when (columns) {
            1 -> 19f
            2 -> 16f
            else -> 13f
        }
        val row = horizontal().apply { gravity = Gravity.TOP }
        for (c in 0 until columns) {
            val col = vertical().apply { setPadding(if (c == 0) 0 else dp(10), 0, 0, 0) }
            for (i in c * perColumn until minOf(items.length(), (c + 1) * perColumn)) {
                col.add(stockRow(items.getJSONObject(i), size), 6)
            }
            row.add(col, weight = 1f)
        }
        box.add(row, 2)
        return box
    }

    private fun stockRow(q: JSONObject, size: Float): View {
        val line = horizontal()
        line.add(text(size, bold = true).apply { text = q.optString("symbol") }, weight = 1f)
        val price = q.num("price")
        if (price == null) {
            line.add(text(size, Colors.DIM).apply { text = "—" })
            return line
        }
        val pct = q.num("changePct") ?: 0.0
        val color = Fmt.changeColor(pct)
        line.add(text(size, color).apply { text = Fmt.int(price) })
        line.add(
            text(size * 0.85f, color).apply {
                text = " ${Fmt.arrow(pct)}${Fmt.signed(pct)}%"
                gravity = Gravity.END
            },
        )
        return line
    }

    private fun subtitle(t: String): TextView = text(13f, Colors.ACCENT, bold = true).apply { text = t }

    private fun pricesPage(): View {
        val box = vertical()
        val gold = section("gold")
        box.add(subtitle("Vàng (triệu đồng/lượng)"))
        if (gold == null || gold.has("error")) {
            box.add(text(13f, Colors.DIM).apply { text = gold?.optString("error") ?: "Chưa có dữ liệu" }, 2)
        } else {
            val rows = gold.optJSONArray("domestic") ?: JSONArray()
            for (i in 0 until minOf(rows.length(), 4)) {
                val r = rows.getJSONObject(i)
                val line = horizontal()
                line.add(text(14f).apply { text = r.optString("name") }, weight = 1f)
                val buy = r.num("buy")?.let { Fmt.million(it) } ?: "--"
                val sell = r.num("sell")?.let { Fmt.million(it) } ?: "--"
                val change = r.num("changeSell") ?: 0.0
                line.add(text(14f, Fmt.changeColor(change)).apply { text = "  $buy / $sell ${Fmt.arrow(change)}" })
                box.add(line, 3)
            }
            gold.optJSONObject("world")?.let { w ->
                val usd = w.num("priceUsdPerOunce")
                if (usd != null) {
                    val vnd = w.num("vndPerTael")?.let { " ≈ ${Fmt.million(it)} tr" } ?: ""
                    box.add(text(13f, Colors.DIM).apply { text = "Thế giới ${Fmt.int(usd)} \$/oz$vnd" }, 3)
                }
            }
        }

        val fuel = section("fuel")
        box.add(subtitle("Xăng dầu Petrolimex (đồng/lít)"), 8)
        if (fuel == null || fuel.has("error")) {
            box.add(text(13f, Colors.DIM).apply { text = fuel?.optString("error") ?: "Chưa có dữ liệu" }, 2)
        } else {
            val rows = fuel.optJSONArray("items") ?: JSONArray()
            for (i in 0 until minOf(rows.length(), 4)) {
                val r = rows.getJSONObject(i)
                val line = horizontal()
                line.add(text(14f).apply { text = r.optString("name") }, weight = 1f)
                val change = r.num("change") ?: 0.0
                val changeText = if (change == 0.0) "" else " (${Fmt.signed(change, 0)})"
                line.add(text(14f, Fmt.changeColor(change)).apply { text = (r.num("price")?.let { Fmt.int(it) } ?: "--") + changeText })
                box.add(line, 3)
            }
        }

        val fx = gold?.optJSONArray("fx")
        if (fx != null && fx.length() > 0) {
            val parts = (0 until fx.length()).mapNotNull { i ->
                val r = fx.getJSONObject(i)
                r.num("sell")?.let { "${r.optString("code")} ${Fmt.int(it)}" }
            }
            box.add(subtitle("Tỷ giá bán Vietcombank"), 8)
            box.add(text(14f).apply { text = parts.joinToString(" · ") }, 3)
        }
        return box
    }

    private fun newsPage(): View {
        val s = section("news")
        sectionError(s)?.let { return it }
        val items = s!!.optJSONArray("items") ?: JSONArray()
        if (items.length() == 0) return message("Chưa có tin.")
        val box = vertical()
        val start = (newsOffset % items.length())
        for (k in 0 until minOf(NEWS_PER_PAGE, items.length())) {
            val item = items.getJSONObject((start + k) % items.length())
            box.add(text(16f, maxLineCount = 2).apply { text = item.optString("title") }, if (k == 0) 0 else 10)
            box.add(text(11f, Colors.DIM).apply { text = item.optString("source") }, 3)
        }
        return box
    }

    private fun forecastPage(): View {
        val w = section("weather")
        sectionError(w)?.let { return it }
        val days = w!!.optJSONArray("daily") ?: JSONArray()
        if (days.length() == 0) return message("Chưa có dự báo.")
        val box = vertical()
        box.add(text(12f, Colors.DIM).apply { text = w.optString("location") })
        for (i in 0 until days.length()) {
            val d = days.getJSONObject(i)
            val line = horizontal()
            line.add(text(16f, bold = true).apply { text = d.optString("weekday").replace("Thứ ", "T") }, weight = 0.9f)
            line.add(text(18f).apply { text = WeatherIcon.of(null, d.optString("description")) })
            line.add(text(14f, Colors.DIM).apply { text = " " + d.optString("description") }, weight = 1.6f)
            val lo = d.num("minC")?.roundToInt()
            val hi = d.num("maxC")?.roundToInt()
            line.add(text(15f).apply { text = "${lo ?: "--"}°–${hi ?: "--"}°" })
            val rain = d.num("rainChance")?.roundToInt()
            line.add(text(13f, Colors.GOOD_DAY).apply { text = if (rain == null) "" else "  ☂$rain%" })
            box.add(line, 12)
        }
        return box
    }
}
