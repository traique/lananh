package vn.lananh.manhinh

import android.content.Context
import android.os.Build
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.security.KeyStore
import java.security.cert.CertificateFactory
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory

class ApiException(val status: Int, val code: String, message: String) : Exception(message)

/**
 * Gọi API của bot (web_app_api.py) bằng HttpURLConnection có sẵn của Android - không
 * thư viện ngoài. HttpURLConnection tự gửi Accept-Encoding: gzip và tự giải nén.
 *
 * Android 6 có kho chứng chỉ gốc cũ, thiếu ISRG Root X1 (Let's Encrypt) và GTS Root
 * (Google Trust Services) mà Render dùng. App mang sẵn các gốc đó (res/raw) và tin
 * chúng CỘNG với kho của hệ thống; kiểm tra tên miền vẫn như bình thường.
 */
object Api {
    private const val CONNECT_TIMEOUT_MS = 20_000
    private const val READ_TIMEOUT_MS = 60_000
    private val BUNDLED_ROOTS = intArrayOf(
        R.raw.isrg_root_x1,
        R.raw.isrg_root_x2,
        R.raw.gts_root_r1,
        R.raw.gts_root_r3,
        R.raw.gts_root_r4,
    )

    @Volatile
    private var socketFactory: SSLSocketFactory? = null

    private fun factory(context: Context): SSLSocketFactory {
        socketFactory?.let { return it }
        synchronized(this) {
            socketFactory?.let { return it }
            val store = KeyStore.getInstance(KeyStore.getDefaultType())
            store.load(null, null)
            try {
                val system = KeyStore.getInstance("AndroidCAStore")
                system.load(null, null)
                for (alias in system.aliases().toList()) {
                    system.getCertificate(alias)?.let { store.setCertificateEntry("sys:$alias", it) }
                }
            } catch (_: Exception) {
                // Không đọc được kho hệ thống: vẫn còn các gốc mang theo.
            }
            val cf = CertificateFactory.getInstance("X.509")
            for (res in BUNDLED_ROOTS) {
                context.resources.openRawResource(res).use { input ->
                    cf.generateCertificates(input).forEachIndexed { i, cert ->
                        store.setCertificateEntry("app:$res:$i", cert)
                    }
                }
            }
            val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
            tmf.init(store)
            val ssl = SSLContext.getInstance("TLS")
            ssl.init(null, tmf.trustManagers, null)
            return ssl.socketFactory.also { socketFactory = it }
        }
    }

    /** "lananh.onrender.com/" -> "https://lananh.onrender.com". */
    fun normalizeServer(raw: String): String {
        var url = raw.trim().trimEnd('/')
        if (url.isEmpty()) return ""
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "https://$url"
        return url
    }

    private fun request(
        context: Context,
        method: String,
        url: String,
        token: String? = null,
        body: JSONObject? = null,
    ): JSONObject {
        val conn = URL(url).openConnection() as HttpURLConnection
        if (conn is HttpsURLConnection) conn.sslSocketFactory = factory(context)
        conn.connectTimeout = CONNECT_TIMEOUT_MS
        conn.readTimeout = READ_TIMEOUT_MS
        conn.requestMethod = method
        conn.useCaches = false
        conn.setRequestProperty("Accept", "application/json")
        conn.setRequestProperty(
            "User-Agent",
            "LananhManHinh/${BuildConfigCompat.versionName(context)} (Android ${Build.VERSION.RELEASE})",
        )
        if (token != null) conn.setRequestProperty("Authorization", "Bearer $token")
        try {
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = conn.responseCode
            val stream = if (status in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            if (status !in 200..299) {
                val err = try {
                    JSONObject(text).optJSONObject("error")
                } catch (_: Exception) {
                    null
                }
                throw ApiException(
                    status,
                    err?.optString("code").orEmpty().ifEmpty { "HTTP_$status" },
                    err?.optString("message").orEmpty().ifEmpty { "Máy chủ trả lỗi $status." },
                )
            }
            return JSONObject(text)
        } finally {
            conn.disconnect()
        }
    }

    /** Đổi mã ghép (/app moi trên Telegram) lấy token. */
    fun pair(context: Context, server: String, code: String): JSONObject =
        request(context, "POST", "$server/api/v1/app/pair", body = JSONObject().put("code", code))

    fun dashboard(context: Context, server: String, token: String, place: String): JSONObject {
        val query = if (place.isBlank()) "" else "?place=" + URLEncoder.encode(place, "UTF-8")
        return request(context, "GET", "$server/api/v1/app/dashboard$query", token = token)
    }
}

/** Bản dữ liệu cuối cùng lấy được, lưu file để mất mạng/khởi động lại vẫn hiện ngay. */
object Store {
    private const val FILE = "dashboard.json"
    val SECTIONS = arrayOf("weather", "gold", "fuel", "news", "watchlist")

    fun load(context: Context): JSONObject? = try {
        val f = File(context.filesDir, FILE)
        if (f.exists()) JSONObject(f.readText(Charsets.UTF_8)) else null
    } catch (_: Exception) {
        null
    }

    fun saveText(context: Context, text: String) {
        try {
            val tmp = File(context.filesDir, "$FILE.tmp")
            tmp.writeText(text, Charsets.UTF_8)
            tmp.renameTo(File(context.filesDir, FILE))
        } catch (_: Exception) {
            // Hết chỗ trống: lần sau lưu lại, màn hình vẫn có dữ liệu trong RAM.
        }
    }

    fun clear(context: Context) {
        File(context.filesDir, FILE).delete()
    }

    /** Phần nào bản mới báo lỗi mà bản cũ có dữ liệu thì giữ bản cũ (đánh dấu "stale"). */
    fun merge(old: JSONObject?, fresh: JSONObject): JSONObject {
        if (old == null) return fresh
        for (key in SECTIONS) {
            val n = fresh.optJSONObject(key)
            val o = old.optJSONObject(key)
            if (n != null && n.has("error") && o != null && !o.has("error")) {
                o.put("stale", true)
                fresh.put(key, o)
            }
        }
        return fresh
    }
}

object BuildConfigCompat {
    fun versionName(context: Context): String = try {
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
    } catch (_: Exception) {
        "?"
    }
}
