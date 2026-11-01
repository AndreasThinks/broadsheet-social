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

// CI supplies a monotonic GitHub Actions run number for published APKs. Local builds use
// commit time as a deterministic fallback; it is not used for releases.
val buildVersionCode = providers.environmentVariable("VERSION_CODE").orNull?.toIntOrNull()
    ?: ((gitCommitTime() - 1704067200) / 60).coerceAtLeast(1)

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
        // Release artifacts must never silently fall back to the public debug signing key.
        create("release") {
            storeFile = file(providers.environmentVariable("ANDROID_KEYSTORE_FILE").orElse("missing-release.keystore").get())
            storePassword = providers.environmentVariable("ANDROID_KEYSTORE_PASSWORD").orElse("").get()
            keyAlias = providers.environmentVariable("ANDROID_KEY_ALIAS").orElse("").get()
            keyPassword = providers.environmentVariable("ANDROID_KEY_PASSWORD").orElse("").get()
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
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
    testImplementation("junit:junit:4.13.2")
}
