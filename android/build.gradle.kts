// Phiên bản cố định: app chạy trên máy Android 6 (minSdk 23) nên KHÔNG dùng thư viện
// AndroidX (bản mới đang nâng minSdk). Chỉ cần Android Gradle Plugin + Kotlin.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
