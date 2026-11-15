import java.util.Properties

plugins { id("com.android.application") }

// Release signing comes from a file outside the repo (never committed):
// ~/.config/sentinel-bigeye/android/signing.properties  (storeFile, storePassword, keyAlias, keyPassword)
val signingFile = file(System.getProperty("user.home") + "/.config/sentinel-bigeye/android/signing.properties")
val signing = Properties().apply { if (signingFile.exists()) signingFile.inputStream().use { load(it) } }

android {
    namespace = "no.sentinel.bigeye"
    compileSdk = 36
    defaultConfig {
        applicationId = "no.sentinel.bigeye"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.1"
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
