// Vira Companion app module. Deliberately small dependency set: CameraX +
// zxing-core for QR pairing; everything else is platform APIs
// (HttpURLConnection, org.json, SharedPreferences, classic Views).
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "nyc.durham.vira.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "nyc.durham.vira.companion"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    // QR pairing: camera preview + frame analysis + the decoder itself.
    // ComponentActivity (androidx.activity) is the LifecycleOwner CameraX
    // binds to; everything else in the app is a plain android.app.Activity.
    val camerax = "1.3.4"
    implementation("androidx.activity:activity:1.9.2")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")
    implementation("com.google.zxing:core:3.5.3")

    // Unit tests run on the JVM: real org.json instead of the android.jar
    // stubs, plain JUnit 4.
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
