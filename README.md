# email-sync

Railway-ready Google Workspace Gmail backup sync to MinIO.

The app runs `gwbackupy` for each Workspace mailbox using a domain-wide delegated Google service account, stores the versioned local backup in `DATA_DIR`, and mirrors it to a MinIO bucket under one prefix per mailbox. Local `gwbackupy` message files stay as `.eml.gz`, but MinIO receives decompressed `.eml` objects with `message/rfc822` content type. In MinIO, files are grouped by Gmail message ID so each message's `.eml` and `.json` metadata sit next to each other:

```text
s3://<MINIO_BUCKET>/<MINIO_PREFIX>/<user@example.com>/gmail/<gmail-message-id>/*.eml
s3://<MINIO_BUCKET>/<MINIO_PREFIX>/<user@example.com>/gmail/<gmail-message-id>/*.json
```

By default it runs continuously every 12 hours. Set `RUN_ONCE=true` if you prefer Railway Cron or one-shot jobs.

## Implementation plan

1. Load Railway environment variables and service-account credentials.
2. Discover mailboxes either from `WORKSPACE_EMAILS` or from the Google Admin Directory API for `WORKSPACE_DOMAIN`.
3. For each active mailbox, impersonate that mailbox with `gwbackupy` and write a local Gmail backup to `DATA_DIR/<email>/gmail`.
4. Upload new or changed backup files to MinIO under `<MINIO_PREFIX>/<email>/gmail/<gmail-message-id>/...`, decompressing `.eml.gz` messages into `.eml` objects during upload.
5. Repeat every `SYNC_INTERVAL_SECONDS` seconds, defaulting to 12 hours.

## Google Workspace setup

Create a Google Cloud service account with a JSON key and enable domain-wide delegation.

Required APIs:

- Gmail API
- Admin SDK API, only needed when using `WORKSPACE_DOMAIN` discovery

Required OAuth scopes for domain-wide delegation:

```text
https://mail.google.com/
https://www.googleapis.com/auth/admin.directory.user.readonly
```

`https://mail.google.com/` is used by `gwbackupy` to read Gmail messages and labels. The Admin Directory scope is used only to list Workspace users. If you set `WORKSPACE_EMAILS`, mailbox discovery does not call the Admin SDK, but the Gmail scope is still required.

## Railway variables

Required:

| Variable | Description |
| --- | --- |
| `MINIO_ENDPOINT` | MinIO host, e.g. `minio.example.com` or `minio.example.com:9000`. Do not include `https://`. |
| `MINIO_ACCESS_KEY` | MinIO access key. |
| `MINIO_SECRET_KEY` | MinIO secret key. |
| `MINIO_BUCKET` | Destination bucket. Created automatically if missing. |
| `GOOGLE_SERVICE_ACCOUNT_KEY_JSON` | Full service-account JSON key. Use this, `GOOGLE_SERVICE_ACCOUNT_KEY_BASE64`, or `GOOGLE_SERVICE_ACCOUNT_KEY_FILE`. |

Mailbox selection, choose one:

| Variable | Description |
| --- | --- |
| `WORKSPACE_DOMAIN` | Workspace domain to discover active users from, e.g. `example.com`. Requires `WORKSPACE_ADMIN_EMAIL`. |
| `WORKSPACE_ADMIN_EMAIL` | Super admin or delegated admin email used to impersonate Admin Directory API access. |
| `WORKSPACE_EMAILS` | Comma-separated explicit mailbox list, e.g. `a@example.com,b@example.com`. If set, discovery is skipped. |
| `EXCLUDED_USERS` | Comma-separated mailbox list to skip during `WORKSPACE_DOMAIN` discovery, e.g. `noreply@example.com,archive@example.com`. |

Optional:

| Variable | Default | Description |
| --- | --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_KEY_BASE64` | unset | Base64-encoded service-account JSON alternative. |
| `GOOGLE_SERVICE_ACCOUNT_KEY_FILE` | unset | Path to a service-account JSON file alternative. |
| `GOOGLE_SERVICE_ACCOUNT_EMAIL` | unset | Passed through to `gwbackupy`; normally not needed for JSON keys. |
| `DATA_DIR` | `./data` | Local backup/cache directory. Use a Railway volume here for incremental backups. |
| `MINIO_SECURE` | `true` | Use HTTPS for MinIO. Set `false` for plain HTTP. |
| `MINIO_PREFIX` | unset | Optional bucket prefix before the mailbox email. |
| `GWBACKUPY_BATCH_SIZE` | `5` | Per-mailbox Gmail worker concurrency. |
| `GWBACKUPY_AUTO_BATCH` | `true` | Let `gwbackupy` adjust concurrency around rate limits. |
| `GWBACKUPY_QUICK_SYNC` | `true` | Skip re-downloading already backed-up messages when local data is present. |
| `GWBACKUPY_QUICK_SYNC_DAYS` | unset | Re-check metadata/labels for recent existing messages. Useful with `7` or `30`. |
| `SYNC_INTERVAL_SECONDS` | `43200` | Delay between runs. |
| `RUN_ONCE` | `false` | Run one sync and exit. Use for Railway Cron. |
| `REMOVE_LOCAL_AFTER_UPLOAD` | `false` | Delete local mailbox backup after upload. This disables incremental local quick-sync benefits. |
| `LOG_LEVEL` | `INFO` | Python log level. |

## Running locally

```bash
uv sync
uv run python main.py
```

For a one-time test:

```bash
RUN_ONCE=true WORKSPACE_EMAILS=user@example.com uv run python main.py
```

## Railway deployment notes

- Start command: `uv run python main.py`
- Recommended: mount a Railway volume and set `DATA_DIR` to a path on that volume, such as `/data/email-sync`. Without persistent storage, each container restart has to rebuild local backup state before uploading.
- If using Railway Cron instead of the built-in loop, set `RUN_ONCE=true` and schedule the service every 12 hours.
- Keep the service-account JSON in Railway variables only; do not commit key files.
