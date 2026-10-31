plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Ký bản release bằng keystore riêng khi có biến môi trường (CI giải mã từ GitHub
// Secrets). Không có thì ký bằng debug key để vẫn cài được - nhưng mỗi máy build có
// debug key khác nhau, cài bản mới phải gỡ bản cũ trước (xem android/README.md).
val keystorePath: String? = System.getenv("ANDROID_KEYSTORE_PATH")

android {
    namespace = "vn.lananh.manhinh"
    compileSdk = 35

    defaultConfig {
        applicationId = "vn.lananh.manhinh"
        minSdk = 23
        targetSdk = 34
        versionCode = (System.getenv("APP_VERSION_CODE") ?: "1").toInt()
        versionName = "1.0.0"
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig =
                if (keystorePath != null) {
                    signingConfigs.getByName("release")
                } else {
                    signingConfigs.getByName("debug")
                }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    lint {
        // Cố ý: wake lock không hẹn giờ (WakeService giữ CPU để đọc cảm biến khi tắt
        // màn hình), cờ FULL_WAKE_LOCK cũ (cách bật màn hình chạy được trên Android 6).
        disable += setOf("Wakelock", "WakelockTimeout", "Deprecated")
        abortOnError = false
        checkReleaseBuilds = false
    }
}
