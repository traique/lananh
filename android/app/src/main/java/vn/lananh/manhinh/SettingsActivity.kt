package vn.lananh.manhinh

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.util.TypedValue
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors
import kotlin.math.roundToInt

/** Cài đặt: ghép với bot bằng mã từ lệnh /app moi, và cách màn hình sáng/tắt. */
class SettingsActivity : Activity() {
    private lateinit var prefs: Prefs
    private val io = Executors.newSingleThreadExecutor()

    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var pairState: TextView
    private lateinit var pairButton: Button
    private lateinit var place: EditText
    private lateinit var alwaysOn: CheckBox
    private lateinit var dayStart: EditText
    private lateinit var dayEnd: EditText
    private lateinit var idle: EditText
    private lateinit var brightness: EditText
    private lateinit var rotate: EditText
    private lateinit var proximity: CheckBox

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildLayout())
        load()
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).roundToInt()

    private fun LinearLayout.label(text: String, topDp: Int = 16, sizeSp: Float = 15f): TextView {
        val tv = TextView(this@SettingsActivity).apply {
            this.text = text
            setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        }
        addView(tv, wrap().apply { topMargin = dp(topDp) })
        return tv
    }

    private fun LinearLayout.note(text: String): TextView = label(text, 2, 12f).apply { alpha = 0.7f }

    private fun LinearLayout.input(hint: String, type: Int = InputType.TYPE_CLASS_TEXT): EditText {
        val et = EditText(this@SettingsActivity).apply {
            this.hint = hint
            inputType = type
            setSingleLine(true)
        }
        addView(et, wrap())
        return et
    }

    private fun LinearLayout.check(text: String): CheckBox {
        val cb = CheckBox(this@SettingsActivity).apply { this.text = text }
        addView(cb, wrap().apply { topMargin = dp(12) })
        return cb
    }

    private fun LinearLayout.button(text: String, onClick: () -> Unit): Button {
        val b = Button(this@SettingsActivity).apply {
            this.text = text
            setOnClickListener { onClick() }
        }
        addView(b, wrap().apply { topMargin = dp(8) })
        return b
    }

    private fun wrap() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    )

    private fun number() = InputType.TYPE_CLASS_NUMBER

    private fun buildLayout(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), dp(32))
        }
        box.label("1. Ghép với bot", 8, 18f)
        box.note("Trên Telegram, nhắn bot: /app moi Phòng khách - bot trả mã ghép 8 ký tự (hết hạn sau 10 phút).")
        box.label("Địa chỉ bot")
        server = box.input("vd lananh.onrender.com", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        box.label("Mã ghép")
        code = box.input(
            "vd K7QF-3MXA",
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS,
        )
        pairButton = box.button("Ghép") { pair() }
        pairState = box.label("", 8, 14f)
        box.button("Bỏ ghép trên máy này") { unpair() }

        box.label("2. Thời tiết", 28, 18f)
        place = box.input("Để trống = thành phố mặc định trong bot")

        box.label("3. Màn hình", 28, 18f)
        alwaysOn = box.check("Luôn sáng 24/24")
        box.label("Giờ ngày (luôn sáng): từ giờ … đến giờ …")
        val hours = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        dayStart = EditText(this).apply {
            inputType = number()
            hint = "6"
        }
        dayEnd = EditText(this).apply {
            inputType = number()
            hint = "22"
        }
        hours.addView(dayStart, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        hours.addView(TextView(this).apply { text = "  đến  " })
        hours.addView(dayEnd, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(hours, wrap())
        box.label("Giờ đêm: tự tắt sau bao nhiêu phút không chạm / không vẫy tay")
        idle = box.input("2", number())
        box.label("Độ sáng giờ đêm (%)")
        brightness = box.input("15", number())
        box.label("Đổi trang bên phải sau (giây)")
        rotate = box.input("20", number())
        box.note(
            "Màn hình tự tắt theo \"Thời gian chờ màn hình\" của máy (Cài đặt > Màn hình). " +
                "Nên đặt 15 hoặc 30 giây và khoá màn hình là \"Không\" hoặc \"Vuốt\".",
        )

        box.label("4. Cảm biến tiệm cận", 28, 18f)
        proximity = box.check("Đưa tay lại gần cảm biến (cạnh loa thoại) để bật màn hình")
        box.note(sensorInfo())
        box.button("Cho app chạy nền (tắt tối ưu pin)") { askIgnoreBattery() }
        box.note(
            "Samsung: vào thêm Smart Manager > Pin > Ứng dụng chưa giám sát và thêm app này " +
                "để máy không tự tắt dịch vụ đánh thức.",
        )

        box.button("Lưu") { save() }
        box.note("Màn hình chính: giữ lâu vào đồng hồ để mở lại trang này.")
        return ScrollView(this).apply { addView(box) }
    }

    private fun sensorInfo(): String {
        val sm = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        val wake = sm.getDefaultSensor(Sensor.TYPE_PROXIMITY, true)
        val any = wake ?: sm.getDefaultSensor(Sensor.TYPE_PROXIMITY)
        return when {
            any == null -> "Máy này không có cảm biến tiệm cận - chạm vào màn hình để bật."
            wake != null -> "Cảm biến: ${any.name} (loại đánh thức được máy)."
            else -> "Cảm biến: ${any.name}. App giữ CPU thức để đọc cảm biến khi tắt màn hình; " +
                "nếu vẫy tay không bật được thì máy đã tắt cảm biến - dùng nút nguồn."
        }
    }

    private fun load() {
        server.setText(prefs.serverUrl)
        place.setText(prefs.place)
        alwaysOn.isChecked = prefs.alwaysOn
        dayStart.setText(prefs.dayStart.toString())
        dayEnd.setText(prefs.dayEnd.toString())
        idle.setText(prefs.idleMinutes.toString())
        brightness.setText(prefs.nightBrightness.toString())
        rotate.setText(prefs.rotateSeconds.toString())
        proximity.isChecked = prefs.proximityWake
        showPairState()
    }

    private fun showPairState() {
        pairState.text = if (prefs.paired) {
            "✅ Đã ghép: ${prefs.deviceName.ifBlank { "thiết bị" }} với ${prefs.serverUrl}"
        } else {
            "Chưa ghép."
        }
    }

    private fun EditText.intOr(default: Int): Int = text.toString().trim().toIntOrNull() ?: default

    private fun save() {
        val newPlace = place.text.toString().trim()
        if (newPlace != prefs.place) prefs.forceRefresh = true
        prefs.place = newPlace
        prefs.alwaysOn = alwaysOn.isChecked
        prefs.dayStart = dayStart.intOr(6)
        prefs.dayEnd = dayEnd.intOr(22)
        prefs.idleMinutes = idle.intOr(2)
        prefs.nightBrightness = brightness.intOr(15)
        prefs.rotateSeconds = rotate.intOr(20)
        prefs.proximityWake = proximity.isChecked
        WakeService.sync(this)
        Toast.makeText(this, "Đã lưu", Toast.LENGTH_SHORT).show()
        finish()
    }

    private fun pair() {
        val url = Api.normalizeServer(server.text.toString())
        val raw = code.text.toString().trim()
        if (url.isEmpty() || raw.isEmpty()) {
            pairState.text = "Nhập địa chỉ bot và mã ghép."
            return
        }
        pairButton.isEnabled = false
        pairState.text = "Đang ghép…"
        val ctx = applicationContext
        io.execute {
            val result = try {
                Result.success(Api.pair(ctx, url, raw))
            } catch (e: Exception) {
                Result.failure(e)
            }
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                pairButton.isEnabled = true
                result.onSuccess { json ->
                    prefs.serverUrl = url
                    prefs.token = json.optString("token")
                    prefs.deviceName = json.optJSONObject("device")?.optString("name").orEmpty()
                    prefs.forceRefresh = true
                    Store.clear(ctx)
                    code.setText("")
                    showPairState()
                }.onFailure { e ->
                    pairState.text = "❌ " + when (e) {
                        is ApiException -> e.message
                        is javax.net.ssl.SSLException -> "Lỗi chứng chỉ HTTPS: ${e.message}"
                        is java.net.UnknownHostException -> "Không tìm thấy máy chủ, kiểm tra địa chỉ và mạng."
                        else -> "Không kết nối được (${e.javaClass.simpleName}: ${e.message})"
                    }
                }
            }
        }
    }

    private fun unpair() {
        prefs.token = ""
        prefs.deviceName = ""
        prefs.forceRefresh = true
        Store.clear(this)
        showPairState()
    }

    @SuppressLint("BatteryLife")
    private fun askIgnoreBattery() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "App đã được phép chạy nền.", Toast.LENGTH_SHORT).show()
            return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")),
            )
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }
}
