# Android backup and restore

Use **Back up** or **Restore** in the app and choose a document with Android's system picker. Backups are JSON envelopes with a version and SHA-256 integrity check containing a SQLite `VACUUM INTO` consistent database snapshot. Restores are bounded to 64 MiB of database content, validated for checksum, SQLite integrity and Broadsheet schema, and staged rather than written into the running database. Restart the app to apply a staged restore before Python initializes its database.

**Sensitive data:** backup files include account credentials and access tokens, private posts, app settings and other personal data. Keep files in a secure location; sharing or uploading a backup can expose all linked accounts. Restore replaces the current local database.
