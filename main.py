from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from google.oauth2 import service_account
from googleapiclient.discovery import build
from gwbackupy.gmail import Gmail
from gwbackupy.providers.gapi_gmail_service_wrapper import GapiGmailServiceWrapper
from gwbackupy.providers.gmail_service_provider import GmailServiceProvider
from gwbackupy.storage.file_storage import FileLink, FileStorage
from minio import Minio
from minio.error import S3Error

LOGGER = logging.getLogger(__name__)
ADMIN_DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
DEFAULT_GWBACKUPY_BATCH_SIZE = 5
DEFAULT_SYNC_INTERVAL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class Settings:
    workspace_domain: str | None
    workspace_admin_email: str | None
    workspace_emails: tuple[str, ...]
    excluded_users: tuple[str, ...]
    service_account_key_path: Path
    service_account_email: str | None
    data_dir: Path
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    minio_prefix: str
    batch_size: int
    auto_batch: bool
    quick_sync: bool
    quick_sync_days: int | None
    sync_interval_seconds: int
    run_once: bool
    remove_local_after_upload: bool
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        service_account_key_path = _resolve_service_account_key()
        workspace_emails = _parse_csv(os.getenv("WORKSPACE_EMAILS", ""))
        excluded_users = _parse_csv(os.getenv("EXCLUDED_USERS", ""))
        workspace_domain = _optional_env("WORKSPACE_DOMAIN")
        workspace_admin_email = _optional_env("WORKSPACE_ADMIN_EMAIL")

        if not workspace_emails and not workspace_domain:
            raise ConfigError(
                "Set WORKSPACE_EMAILS or WORKSPACE_DOMAIN. "
                "WORKSPACE_DOMAIN also requires WORKSPACE_ADMIN_EMAIL for user discovery."
            )
        if workspace_domain and not workspace_admin_email:
            raise ConfigError("WORKSPACE_ADMIN_EMAIL is required when WORKSPACE_DOMAIN is set.")

        return cls(
            workspace_domain=workspace_domain,
            workspace_admin_email=workspace_admin_email,
            workspace_emails=tuple(workspace_emails),
            excluded_users=tuple(email.lower() for email in excluded_users),
            service_account_key_path=service_account_key_path,
            service_account_email=_optional_env("GOOGLE_SERVICE_ACCOUNT_EMAIL"),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
            minio_endpoint=_required_env("MINIO_ENDPOINT"),
            minio_access_key=_required_env("MINIO_ACCESS_KEY"),
            minio_secret_key=_required_env("MINIO_SECRET_KEY"),
            minio_bucket=_required_env("MINIO_BUCKET"),
            minio_secure=_bool_env("MINIO_SECURE", True),
            minio_prefix=os.getenv("MINIO_PREFIX", "").strip("/"),
            batch_size=_int_env("GWBACKUPY_BATCH_SIZE", DEFAULT_GWBACKUPY_BATCH_SIZE),
            auto_batch=_bool_env("GWBACKUPY_AUTO_BATCH", True),
            quick_sync=_bool_env("GWBACKUPY_QUICK_SYNC", True),
            quick_sync_days=_optional_int_env("GWBACKUPY_QUICK_SYNC_DAYS"),
            sync_interval_seconds=_int_env("SYNC_INTERVAL_SECONDS", DEFAULT_SYNC_INTERVAL_SECONDS),
            run_once=_bool_env("RUN_ONCE", False),
            remove_local_after_upload=_bool_env("REMOVE_LOCAL_AFTER_UPLOAD", False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


class ConfigError(RuntimeError):
    pass


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(levelname)s %(asctime)s - %(message)s",
    )

    LOGGER.info("Starting email sync service")
    if settings.run_once:
        run_sync(settings)
        return

    while True:
        started_at = time.monotonic()
        try:
            run_sync(settings)
        except Exception:
            LOGGER.exception("Sync run failed")

        elapsed = time.monotonic() - started_at
        sleep_for = max(0, settings.sync_interval_seconds - int(elapsed))
        LOGGER.info("Next sync run in %s seconds", sleep_for)
        time.sleep(sleep_for)


def run_sync(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    minio_client = _build_minio_client(settings)
    _ensure_bucket(minio_client, settings.minio_bucket)

    emails = list_mailboxes(settings)
    if not emails:
        raise RuntimeError("No mailbox emails found to sync")

    LOGGER.info("Syncing %s mailbox(es)", len(emails))
    for email in emails:
        sync_mailbox(email, settings, minio_client)

    LOGGER.info("Sync run completed")


def list_mailboxes(settings: Settings) -> list[str]:
    explicit_emails = list(settings.workspace_emails)
    if explicit_emails:
        LOGGER.info("Using %s explicitly configured mailbox(es)", len(explicit_emails))
        return sorted(set(explicit_emails))

    if not settings.workspace_domain or not settings.workspace_admin_email:
        return []

    LOGGER.info("Discovering active users for domain %s", settings.workspace_domain)
    excluded_users = set(settings.excluded_users)
    credentials = service_account.Credentials.from_service_account_file(
        settings.service_account_key_path,
        scopes=[ADMIN_DIRECTORY_SCOPE],
        subject=settings.workspace_admin_email,
    )
    service = build("admin", "directory_v1", credentials=credentials, cache_discovery=False)

    emails: list[str] = []
    request = service.users().list(
        domain=settings.workspace_domain,
        maxResults=500,
        orderBy="email",
        projection="basic",
    )
    while request is not None:
        response = request.execute()
        for user in response.get("users", []):
            if user.get("suspended") or user.get("archived"):
                continue
            primary_email = user.get("primaryEmail")
            if primary_email and primary_email.lower() not in excluded_users:
                emails.append(primary_email)
        request = service.users().list_next(request, response)

    return sorted(set(emails))


def sync_mailbox(email: str, settings: Settings, minio_client: Minio) -> None:
    LOGGER.info("Backing up %s", email)
    mailbox_dir = settings.data_dir / email
    gmail_dir = mailbox_dir / "gmail"
    oauth_tokens_dir = settings.data_dir / "oauth-tokens"

    storage = FileStorage(str(gmail_dir))
    token_storage = FileStorage(str(oauth_tokens_dir))
    service_provider = GmailServiceProvider(
        service_account_email=settings.service_account_email,
        service_account_file_path=str(settings.service_account_key_path),
        storage=token_storage,
    )
    service_wrapper = GapiGmailServiceWrapper(service_provider=service_provider)
    gmail = Gmail(
        email=email,
        service_wrapper=service_wrapper,
        batch_size=settings.batch_size,
        storage=storage,
        auto_batch=settings.auto_batch,
    )

    if not gmail.backup(quick_sync=settings.quick_sync, quick_sync_days=settings.quick_sync_days):
        raise RuntimeError(f"gwbackupy backup failed for {email}")

    uploaded = upload_mailbox_backup(email, mailbox_dir, settings, minio_client)
    LOGGER.info("Uploaded %s changed file(s) for %s", uploaded, email)

    if settings.remove_local_after_upload:
        shutil.rmtree(mailbox_dir, ignore_errors=True)
        LOGGER.info("Removed local backup directory for %s", email)


def upload_mailbox_backup(
    email: str,
    mailbox_dir: Path,
    settings: Settings,
    minio_client: Minio,
) -> int:
    uploaded = 0
    if not mailbox_dir.exists():
        return uploaded

    for path in sorted(p for p in mailbox_dir.rglob("*") if p.is_file()):
        relative_path = path.relative_to(mailbox_dir)
        if path.name.endswith(".eml.gz"):
            uploaded += upload_decompressed_eml(
                path=path,
                relative_path=relative_path,
                email=email,
                settings=settings,
                minio_client=minio_client,
            )
            continue

        object_name = _object_name(settings.minio_prefix, email, _bucket_relative_path(relative_path))
        stat = path.stat()
        if _remote_matches(minio_client, settings.minio_bucket, object_name, stat.st_size):
            continue
        minio_client.fput_object(
            bucket_name=settings.minio_bucket,
            object_name=object_name,
            file_path=str(path),
            content_type="application/json",
        )
        uploaded += 1
    return uploaded


def upload_decompressed_eml(
    path: Path,
    relative_path: Path,
    email: str,
    settings: Settings,
    minio_client: Minio,
) -> int:
    object_relative_path = _bucket_relative_path(relative_path, decompress_eml=True)
    object_name = _object_name(settings.minio_prefix, email, object_relative_path)

    with tempfile.NamedTemporaryFile() as decompressed_file:
        with gzip.open(path, "rb") as compressed_file:
            shutil.copyfileobj(compressed_file, decompressed_file)
        decompressed_file.flush()
        size = Path(decompressed_file.name).stat().st_size

        if _remote_matches(minio_client, settings.minio_bucket, object_name, size):
            return 0

        minio_client.fput_object(
            bucket_name=settings.minio_bucket,
            object_name=object_name,
            file_path=decompressed_file.name,
            content_type="message/rfc822",
        )
    return 1


def _remote_matches(client: Minio, bucket: str, object_name: str, size: int) -> bool:
    try:
        stat = client.stat_object(bucket, object_name)
        return stat.size == size
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            return False
        raise


def _build_minio_client(settings: Settings) -> Minio:
    return Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _ensure_bucket(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        LOGGER.info("Created MinIO bucket %s", bucket)


def _object_name(prefix: str, email: str, relative_path: Path) -> str:
    safe_parts = [email, *relative_path.parts]
    name = "/".join(safe_parts)
    return f"{prefix}/{name}" if prefix else name


def _bucket_relative_path(relative_path: Path, decompress_eml: bool = False) -> Path:
    service = relative_path.parts[0] if relative_path.parts else "gmail"
    filename = relative_path.name
    if decompress_eml and filename.endswith(".eml.gz"):
        filename = filename.removesuffix(".gz")

    parsed = FileLink.parse_file_name(relative_path.name)
    if parsed is None:
        return Path(service) / filename

    object_id = quote(str(parsed["object_id"]), safe="")
    return Path(service) / object_id / filename


def _resolve_service_account_key() -> Path:
    key_path = _optional_env("GOOGLE_SERVICE_ACCOUNT_KEY_FILE")
    if key_path:
        return Path(key_path).resolve()

    raw_json = _optional_env("GOOGLE_SERVICE_ACCOUNT_KEY_JSON")
    raw_base64 = _optional_env("GOOGLE_SERVICE_ACCOUNT_KEY_BASE64")
    if not raw_json and not raw_base64:
        raise ConfigError(
            "Set GOOGLE_SERVICE_ACCOUNT_KEY_FILE, GOOGLE_SERVICE_ACCOUNT_KEY_JSON, "
            "or GOOGLE_SERVICE_ACCOUNT_KEY_BASE64."
        )

    if raw_base64:
        raw_json = base64.b64decode(raw_base64).decode("utf-8")

    try:
        json.loads(raw_json or "")
    except json.JSONDecodeError as exc:
        raise ConfigError("Google service-account key is not valid JSON") from exc

    temp_dir = Path(tempfile.gettempdir()) / "email-sync"
    temp_dir.mkdir(parents=True, exist_ok=True)
    key_file = temp_dir / "service-account.json"
    key_file.write_text(raw_json or "", encoding="utf-8")
    key_file.chmod(0o600)
    return key_file


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _required_env(name: str) -> str:
    value = _optional_env(name)
    if value is None:
        raise ConfigError(f"{name} is required")
    return value


def _bool_env(name: str, default: bool) -> bool:
    value = _optional_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    value = _optional_env(name)
    if value is None:
        return default
    return int(value)


def _optional_int_env(name: str) -> int | None:
    value = _optional_env(name)
    if value is None:
        return None
    return int(value)


if __name__ == "__main__":
    main()
