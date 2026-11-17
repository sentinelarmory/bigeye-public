import java.util.Properties

plugins { id("com.android.application") }

// Release signing comes from a file outside the repo (never committed), same key as the BigEye phone app:
// ~/.config/sentinel-bigeye/android/signing.properties  (storeFile, storePassword, keyAlias, keyPassword)
val signingFile = file(System.getProperty("user.home") + "/.config/sentinel-bigeye/android/signing.properties")
val signing = Properties().apply { if (signingFile.exists()) signingFile.inputStream().use { load(it) } }

android {
    namespace = "no.sentinel.bigeyecam"
    compileSdk = 36
    defaultConfig {
        applicationId = "no.sentinel.bigeyecam"
        minSdk = 21          // old phones (Android 5) are the point: they become cameras
        targetSdk = 35
        versionCode = 6
        versionName = "1.0.5"
    }
    signingConfigs {
        create("release") {
            if (signing.isNotEmpty()) {
                storeFile = file(signing.getProperty("storeFile"))
                storePassword = signing.getProperty("storePassword")
                keyAlias = signing.getProperty("keyAlias")
                keyPassword = signing.getProperty("keyPassword")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            if (signing.isNotEmpty()) signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
