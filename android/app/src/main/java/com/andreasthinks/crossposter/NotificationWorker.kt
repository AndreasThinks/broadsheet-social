package com.andreasthinks.crossposter

import android.content.Context
import android.util.Log
import androidx.work.Worker
import androidx.work.WorkerParameters

/** Checks every account for new notifications while the app is closed. */
class NotificationWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result = try {
        val py = startPython(applicationContext)
        py.getModule("crossposter_android")
            .callAttr("poll", applicationContext.filesDir.absolutePath, PORT, Notifier(applicationContext))
        Result.success()
    } catch (e: Exception) {
        Log.w("crossposter", "notification check failed", e)
        Result.retry()
    }
}
