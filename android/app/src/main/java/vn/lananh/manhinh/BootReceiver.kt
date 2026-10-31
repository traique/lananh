package vn.lananh.manhinh

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Bật lại màn hình thông tin sau khi khởi động máy hoặc cập nhật app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        WakeService.sync(context)
        if (action == Intent.ACTION_BOOT_COMPLETED) {
            try {
                context.startActivity(
                    Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            } catch (_: Exception) {
                // Android 10+ không cho mở màn hình từ nền: người dùng tự mở app.
            }
        }
    }
}
