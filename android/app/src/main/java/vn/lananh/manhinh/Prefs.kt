package vn.lananh.manhinh

import android.content.Context
import android.content.SharedPreferences
import java.util.Calendar

/** Cài đặt của app (SharedPreferences). Token ghép chỉ lưu trên máy này. */
class Prefs(context: Context) {
    private val sp: SharedPreferences =
        context.getSharedPreferences("lananh_manhinh", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "") ?: ""
        set(v) = sp.edit().putString("server_url", v).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    var deviceName: String
        get() = sp.getString("device_name", "") ?: ""
        set(v) = sp.edit().putString("device_name", v).apply()

    /** Địa điểm thời tiết; rỗng = thành phố mặc định của chủ bot (/thoitiet nha ...). */
    var place: String
        get() = sp.getString("place", "") ?: ""
        set(v) = sp.edit().putString("place", v).apply()

    /** Sáng 24/24, bỏ qua giờ đêm. */
    var alwaysOn: Boolean
        get() = sp.getBoolean("always_on", false)
        set(v) = sp.edit().putBoolean("always_on", v).apply()

    /** Giờ ngày [dayStart, dayEnd): màn hình luôn sáng. Ngoài khung = giờ đêm. */
    var dayStart: Int
        get() = sp.getInt("day_start", 6)
        set(v) = sp.edit().putInt("day_start", v.coerceIn(0, 23)).apply()

    var dayEnd: Int
        get() = sp.getInt("day_end", 22)
        set(v) = sp.edit().putInt("day_end", v.coerceIn(0, 24)).apply()

    /** Giờ đêm: không chạm/không vẫy tay bao nhiêu phút thì cho màn hình tự tắt. */
    var idleMinutes: Int
        get() = sp.getInt("idle_minutes", 2)
        set(v) = sp.edit().putInt("idle_minutes", v.coerceIn(1, 120)).apply()

    /** Độ sáng giờ đêm (%). */
    var nightBrightness: Int
        get() = sp.getInt("night_brightness", 15)
        set(v) = sp.edit().putInt("night_brightness", v.coerceIn(1, 100)).apply()

    var proximityWake: Boolean
        get() = sp.getBoolean("proximity_wake", true)
        set(v) = sp.edit().putBoolean("proximity_wake", v).apply()

    /** Mấy giây đổi trang bên phải một lần. */
    var rotateSeconds: Int
        get() = sp.getInt("rotate_seconds", 20)
        set(v) = sp.edit().putInt("rotate_seconds", v.coerceIn(5, 300)).apply()

    /** Cài đặt vừa đổi (ghép lại, máy chủ, địa điểm): màn hình lấy dữ liệu mới ngay. */
    var forceRefresh: Boolean
        get() = sp.getBoolean("force_refresh", false)
        set(v) = sp.edit().putBoolean("force_refresh", v).apply()

    val paired: Boolean
        get() = serverUrl.isNotBlank() && token.isNotBlank()

    fun isDaytime(now: Calendar = Calendar.getInstance()): Boolean {
        val h = now.get(Calendar.HOUR_OF_DAY)
        val start = dayStart
        val end = dayEnd
        return when {
            start == end -> true
            start < end -> h in start until end
            else -> h >= start || h < end
        }
    }
}
