package com.andreasthinks.crossposter

import android.content.ContentResolver
import android.net.Uri
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** Portable, bounded SQLite backup; never copy a live database file (especially with WAL enabled). */
object BackupRestore {
    const val MAX_BACKUP_BYTES = 64 * 1024 * 1024
    private const val MAGIC = "broadsheet-sqlite-backup"
    private const val VERSION = 1

    fun exportDatabase(database: File, resolver: ContentResolver, destination: Uri) {
        require(database.isFile && database.length() <= MAX_BACKUP_BYTES) { "Database is missing or too large" }
        val snapshot = File.createTempFile("broadsheet-backup-", ".sqlite", database.parentFile)
        try {
            // SQLite's backup API produces a transactionally consistent snapshot, including committed WAL data.
            val source = android.database.sqlite.SQLiteDatabase.openDatabase(database.path, null, android.database.sqlite.SQLiteDatabase.OPEN_READONLY)
            val snapshotPath = snapshot.path + ".snapshot"
            try { source.execSQL("VACUUM INTO '${snapshotPath.replace("'", "''")}'") }
            finally { source.close() }
            val stable = File(snapshotPath)
            val data = stable.readBytes()
            stable.delete()
            require(data.size <= MAX_BACKUP_BYTES) { "Backup exceeds the 64 MiB limit" }
            val envelope = JSONObject().put("format", MAGIC).put("version", VERSION)
                .put("sha256", sha256(data)).put("database", Base64.encodeToString(data, Base64.NO_WRAP))
            resolver.openOutputStream(destination, "w")!!.bufferedWriter(Charsets.UTF_8).use { it.write(envelope.toString()) }
        } finally { snapshot.delete() }
    }

    /** Validate fully, then atomically stage; startup promotes staged DB before opening the live DB. */
    fun stageRestore(database: File, resolver: ContentResolver, sourceUri: Uri) {
        val bytes = resolver.openInputStream(sourceUri)?.use { input ->
            val out = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192); var total = 0
            while (true) { val n = input.read(buffer); if (n < 0) break; total += n; require(total <= MAX_BACKUP_BYTES * 2) { "Backup file is too large" }; out.write(buffer, 0, n) }
            out.toByteArray()
        } ?: throw IllegalArgumentException("Cannot read backup")
        val root = JSONObject(String(bytes, Charsets.UTF_8))
        require(root.optString("format") == MAGIC && root.optInt("version") == VERSION) { "Unsupported backup format" }
        val encoded = root.getString("database")
        require(encoded.length <= MAX_BACKUP_BYTES * 4 / 3 + 16) { "Backup database is too large" }
        val db = Base64.decode(encoded, Base64.DEFAULT)
        require(db.size <= MAX_BACKUP_BYTES && sha256(db) == root.getString("sha256")) { "Backup checksum failed" }
        val tmp = File(database.parentFile, "broadsheet.sqlite.restore.tmp")
        tmp.writeBytes(db)
        val check = android.database.sqlite.SQLiteDatabase.openDatabase(tmp.path, null, android.database.sqlite.SQLiteDatabase.OPEN_READONLY)
        try {
            require(check.rawQuery("PRAGMA integrity_check", null).use { it.moveToFirst() && it.getString(0) == "ok" }) { "Backup database is corrupt" }
            check.rawQuery("SELECT name FROM sqlite_master WHERE type='table' AND name='kv'", null).use { require(it.moveToFirst()) { "Not a Broadsheet database" } }
        } finally { check.close() }
        val staged = File(database.parentFile, "broadsheet.sqlite.restore")
        if (!tmp.renameTo(staged)) { tmp.delete(); throw IllegalStateException("Unable to stage backup") }
    }

    private fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
}
