package com.andreasthinks.crossposter

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.concurrent.TimeUnit

const val PORT = 47616
const val CHANNEL_ID = "notifications"

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(CHANNEL_ID, getString(R.string.channel_name), NotificationManager.IMPORTANCE_DEFAULT)
        channel.description = getString(R.string.channel_description)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)

        // Android runs this at most every 15 minutes, and less often to save battery.
        val work = PeriodicWorkRequestBuilder<NotificationWorker>(15, TimeUnit.MINUTES).build()
        WorkManager.getInstance(this).enqueueUniquePeriodicWork("notifications", ExistingPeriodicWorkPolicy.KEEP, work)
    }
}

/** Start Python and make sure this version's app code is unpacked. Safe to call from any thread, repeatedly. */
@Synchronized
fun startPython(context: Context): Python {
    if (!Python.isStarted()) Python.start(AndroidPlatform(context.applicationContext))
    unpackAppCode(context)
    return Python.getInstance()
}

/** Promote a validated backup before any Python code can open the live database. */
private fun promoteStagedRestore(context: Context) {
    val staged = File(context.filesDir, "broadsheet.sqlite.restore")
    if (!staged.exists()) return
    val live = File(context.filesDir, "broadsheet.sqlite")
    val sidecars = listOf("-wal", "-shm")
    sidecars.forEach { File(live.path + it).delete() }
    val replacement = File(context.filesDir, "broadsheet.sqlite.restore-ready")
    if (!staged.renameTo(replacement)) throw IllegalStateException("Cannot activate staged restore")
    if (live.exists() && !live.delete()) throw IllegalStateException("Cannot replace current database")
    if (!replacement.renameTo(live)) throw IllegalStateException("Cannot activate restored database")
}

/** Copy crossposter/, static/ and feeds/ out of the APK into files/app, once per installed version. */
private fun unpackAppCode(context: Context) {
    val version = context.packageManager.getPackageInfo(context.packageName, 0).longVersionCode.toString()
    promoteStagedRestore(context)
    val target = File(context.filesDir, "app")
    val stamp = File(target, ".version")
    if (stamp.exists() && stamp.readText() == version) return
    target.deleteRecursively()
    copyAsset(context, "app", target)
    stamp.writeText(version)
}

private fun copyAsset(context: Context, path: String, dest: File) {
    val children = context.assets.list(path) ?: emptyArray()
    if (children.isEmpty()) {
        dest.parentFile?.mkdirs()
        context.assets.open(path).use { input -> dest.outputStream().use { input.copyTo(it) } }
        return
    }
    dest.mkdirs()
    for (child in children) copyAsset(context, "$path/$child", File(dest, child))
}
