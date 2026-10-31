package vn.lananh.manhinh

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock

/** Báo cho màn hình đang mở biết có người vừa đưa tay lại gần (cùng tiến trình). */
object WakeBus {
    @Volatile
    var listener: (() -> Unit)? = null
}

/**
 * Dịch vụ chạy nền (có thông báo cố định) lắng nghe cảm biến tiệm cận, kể cả khi
 * màn hình đã tắt:
 * - Giữ PARTIAL_WAKE_LOCK để CPU không ngủ -> cảm biến vẫn gửi sự kiện. Máy cắm sạc
 *   suốt nên tốn thêm chút điện là chấp nhận được (Doze cũng không chạy khi sạc).
 * - Có vật lại gần (xa -> gần): màn hình đang tắt thì bật lên bằng wake lock
 *   ACQUIRE_CAUSES_WAKEUP rồi đưa MainActivity lên trước; đang sáng thì chỉ báo
 *   MainActivity đếm lại thời gian chờ tắt.
 *
 * Một số máy tắt hẳn cảm biến khi tắt màn hình dù giữ wake lock; khi đó app vẫn chạy,
 * chỉ là vẫy tay không bật được màn hình (xem android/README.md).
 */
class WakeService : Service(), SensorEventListener {
    private lateinit var sensors: SensorManager
    private lateinit var power: PowerManager
    private var proximity: Sensor? = null
    private var cpuLock: PowerManager.WakeLock? = null
    private var lastNear = false
    private var lastWakeAt = 0L
    private val main = Handler(Looper.getMainLooper())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        sensors = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        power = getSystemService(Context.POWER_SERVICE) as PowerManager
        goForeground()
        proximity = sensors.getDefaultSensor(Sensor.TYPE_PROXIMITY, true)
            ?: sensors.getDefaultSensor(Sensor.TYPE_PROXIMITY)
        val sensor = proximity
        if (sensor == null) {
            stopSelf()
            return
        }
        cpuLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "lananh:proximity").apply {
            setReferenceCounted(false)
            acquire()
        }
        sensors.registerListener(this, sensor, SensorManager.SENSOR_DELAY_NORMAL)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        sensors.unregisterListener(this)
        cpuLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    override fun onSensorChanged(event: SensorEvent) {
        val sensor = proximity ?: return
        // Cảm biến nhị phân báo 0 (gần) / maximumRange (xa); loại đo khoảng cách báo cm.
        val near = event.values[0] < minOf(sensor.maximumRange, 5f)
        if (near && !lastNear) onWave()
        lastNear = near
    }

    private fun onWave() {
        val now = SystemClock.elapsedRealtime()
        if (now - lastWakeAt < 1500) return
        lastWakeAt = now
        main.post { WakeBus.listener?.invoke() }
        if (power.isInteractive) return
        @Suppress("DEPRECATION")
        val screen = power.newWakeLock(
            PowerManager.FULL_WAKE_LOCK or
                PowerManager.ACQUIRE_CAUSES_WAKEUP or
                PowerManager.ON_AFTER_RELEASE,
            "lananh:wake",
        )
        screen.acquire(5_000)
        val open = Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(MainActivity.EXTRA_WAKE, true)
        try {
            startActivity(open)
        } catch (_: Exception) {
            // Android 10+ có thể chặn mở activity từ nền; màn hình vẫn đã sáng.
        }
    }

    private fun goForeground() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, "Đánh thức màn hình", NotificationManager.IMPORTANCE_MIN),
            )
            Notification.Builder(this, CHANNEL)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this).setPriority(Notification.PRIORITY_MIN)
        }
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = builder
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Lan Anh Màn hình")
            .setContentText("Đưa tay lại gần cảm biến để bật màn hình")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    companion object {
        private const val CHANNEL = "wake"
        private const val NOTIFICATION_ID = 1

        fun sync(context: Context) {
            val intent = Intent(context, WakeService::class.java)
            if (!Prefs(context).proximityWake) {
                context.stopService(intent)
                return
            }
            try {
                if (Build.VERSION.SDK_INT >= 26) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (_: Exception) {
                // Android 12+ chặn khởi động dịch vụ nền trong vài trường hợp; mở app là chạy lại.
            }
        }
    }
}
