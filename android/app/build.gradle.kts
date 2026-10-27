import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The app code is the same crossposter/, static/ and feeds/ as the web and desktop versions,
// copied in as assets at build time and unpacked onto the phone on first launch of each version.
val repoRoot = rootProject.projectDir.parentFile
val appCode = layout.buildDirectory.dir("generated/appcode")

fun gitCommitTime(): Int = try {
    providers.exec { commandLine("git", "log", "-1", "--format=%ct"); workingDir = repoRoot }
        .standardOutput.asText.get().trim().toInt()
} catch (e: Exception) {
    (System.currentTimeMillis() / 1000).toInt()
}

// Commit time in minutes since 2024 keeps versionCode rising with every push to main, within Int range.
val buildVersionCode = ((gitCommitTime() - 1704067200) / 60).coerceAtLeast(1)

val copyAppCode by tasks.registering(Sync::class) {
    from(repoRoot) {
        include("crossposter/**", "static/**", "feeds/**")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    into(appCode.map { it.dir("app") })
}

android {
    namespace = "com.andreasthinks.crossposter"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.andreasthinks.crossposter"
        minSdk = 26
        targetSdk = 35
        versionCode = buildVersionCode
        versionName = providers.environmentVariable("VERSION_NAME").getOrElse("0.1.$buildVersionCode")
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    signingConfigs {
        // A shared debug key, committed so every build (local or CI) can install over the last one.
        getByName("debug") {
            storeFile = rootProject.file("shared-debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
        // For wider distribution, set these from repository secrets to sign with a private key instead.
        // Switching keys means uninstalling once, as Android only accepts updates signed by the same key.
        val keystore = providers.environmentVariable("ANDROID_KEYSTORE_FILE").orNull
        if (keystore != null) {
            create("release") {
                storeFile = file(keystore)
                storePassword = providers.environmentVariable("ANDROID_KEYSTORE_PASSWORD").get()
                keyAlias = providers.environmentVariable("ANDROID_KEY_ALIAS").get()
                keyPassword = providers.environmentVariable("ANDROID_KEY_PASSWORD").get()
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    sourceSets["main"].assets.srcDir(appCode)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }

tasks.named("preBuild") { dependsOn(copyAppCode) }

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            options("--no-deps")
            install("-r", "requirements-android.txt")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
}
