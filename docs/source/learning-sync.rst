Learning sync
=============

Learning sync is optional. It keeps a small set of learning data in a folder
that you choose, so a folder provider such as iCloud Drive, OneDrive, Dropbox,
or a network folder can make it available to another retype installation. On
macOS, create a new folder in iCloud Drive and choose it from **Customisation →
Filesystem → Learning sync**.

retype does not use iCloud account APIs, CloudKit, a retype account, or a
hosted service. It cannot tell whether a provider has uploaded a change to
another device. The status **Local changes are saved in the selected sync
folder** only means that retype completed a local folder write.

Enable and use sync
-------------------

#. Choose **Choose sync folder…** and review the upload scope.
#. Use **Sync now** to scan complete replica files immediately. retype also
   queues local changes and scans without putting provider-folder I/O in the
   typing path.
#. Use **Disable sync on this device** to return to local-only operation. It
   never deletes the selected folder or its data.

The diagnostic button explains waiting-for-provider, missing-book, corrupt
file, rejected-file, and recovery conditions. A folder that is temporarily
unavailable does not prevent local reading or saving; pending learning changes
remain local and are retried when the folder returns.

What synchronizes
-----------------

* Resume progress for byte-identical EPUBs. When installations differ, retype
  opens the **furthest** saved position. A changed EPUB is a different edition;
  its progress is not silently mapped.
* Cumulative chord-mastery word keys and manual mastered/unmastered choices.
  Independent offline practice is combined rather than overwritten.
* Line splits, replacements, automatic newline, chord lesson settings, and
  the stenography map.

The following always stay local: user and library paths, window geometry and
splitters, fonts, icons, themes, keymaps, caches, logs, session statistics,
and the CharaChorder device dictionary.

Managed EPUB library
--------------------

No discovered library path is uploaded. To make a user EPUB available on other
installations, first grant the displayed managed-library consent, then select
**Import EPUB into managed library…**. retype copies only that explicitly
selected book using a SHA-256 content address. Bundled EPUBs in packaged
retype releases are never uploaded. When running from source, the checkout's
``library`` can also hold local EPUBs; an explicitly selected one still needs
managed-library consent before it is copied.

A managed EPUB is limited to 100 MiB and all managed EPUBs in a collection are
limited to 1 GiB. The selected folder's provider/account controls remote
storage encryption and access; retype does not add application-level
end-to-end encryption. Missing, altered, or corrupt managed books are rejected
locally while their progress remains available for a valid matching edition.

Reliability and recovery
------------------------

Each installation writes only its own validated replica file. Complete files
are atomically replaced and rolling local backups plus rejected-file copies are
kept in the local application-data recovery directory. This allows deterministic
merges after offline or simultaneous use, but it is eventual synchronization,
not a lock or an instant cloud guarantee. Do not edit replica files manually.
