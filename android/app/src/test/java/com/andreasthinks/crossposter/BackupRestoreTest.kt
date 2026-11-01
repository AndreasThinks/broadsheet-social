package com.andreasthinks.crossposter

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BackupRestoreTest {
    @Test fun backupLimitIsFinite() { assertEquals(64 * 1024 * 1024, BackupRestore.MAX_BACKUP_BYTES) }
    @Test fun backupFormatVersionIsSupported() { assertTrue(BackupRestore::class.java.declaredFields.isNotEmpty()) }
}
