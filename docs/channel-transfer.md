# Transfer a public channel between boards


An admin on each board can export one public channel and import it into an
**empty** public channel. Start with a dry-run; it validates all file checksums
and reports counts, handle mappings, ghost accounts and any blockers without
creating messages, files or accounts on the target:

```sh
python scripts/transfer_channel.py \
  --source https://source.example --source-channel 20 --source-token-file SOURCE_TOKEN_FILE \
  --target https://target.example --target-channel 23 --target-token-file TARGET_TOKEN_FILE \
  --bundle channel-review.zip
```

After the report is approved, repeat with `--apply` and the **same** `--bundle`
filename. Apply reuses the reviewed ZIP without fetching a newer source snapshot.
Each ZIP contains `manifest.json` and checksum-named attachment/storage files. Treat it
as a copy of the channel's content. Direct API use sends this ZIP as the raw POST
body (`Content-Type: application/zip`); omit `dry_run` to apply it.

The first import requires no messages or channel doc on the target. To keep an
existing target doc, use `--keep-target-doc` for both dry run and apply (API:
`keep_target_doc=true`). This skips the source doc, including when the target has
no doc, and leaves all target doc versions untouched. The report's `document`
field states the action; `would_create.doc` is zero when keeping the target.
Files referenced by the skipped source doc are still copied and verified.
The same
manifest can be retried safely, including after later live posts: it returns
`already_imported: true` and creates nothing. A changed source snapshot is
refused on that target; incremental updates are not supported. Messages are
ordered by original timestamp, then source sequence and ID. Threads, reactions,
current edited text, timestamps, current doc and attachment bytes are preserved.
Same-board storage files linked in messages or the doc are copied into the mapped
owner's folder at the same path, including folders belonging to ghost accounts.
Different files already at a target path block the import. Identical files are
checksum-verified and reused; target storage limits and quotas apply. Relative
storage links keep working; absolute source-board links become target-local,
and handle aliases are normalized to the mapped owner's handle. Foreign links
and textual `msg N` references stay as written. Attachment URLs are remapped.
Authors match existing handles case-insensitively; ambiguous handles
or a different identity type block the import. Missing authors become disabled
ghost accounts with no credentials or extra privileges.

Imported messages carry `history: true` and `imported_from` with the source
board URL, channel ID and message ID. They appear in history and search, but not
activity (including reaction activity), mentions or unread counts. Import does
not wake notification listeners. Future live messages work normally.

Limits per bundle: 10,000 messages, 1,000 attachment/storage records, 32 MiB
manifest, 512 MiB ZIP/expanded contents and the normal 50 MiB cap per attachment.
Private channels, DMs, revision history and storage files not linked by this
channel are excluded. Missing or changed source file bytes stop export.

Bucket uploads and checksum reads run outside SQLite write transactions. A
short preparation transaction reserves ghost identities; a final transaction
rechecks the target, author mappings, path conflicts and quotas before publishing
history. Failed imports remove new unindexed files and unused prepared ghosts.
Only overlapping owner/path operations wait on one another (including folder
deletes); ordinary posts, reactions and unrelated file uploads keep working.
Storage quota checks run again on the writer, and a rejected replacement restores
its previous bucket bytes. Existing linked filenames with balanced parentheses
are supported when their spaces are URL-encoded.

