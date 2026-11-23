#!/usr/bin/env python3
"""
Sync media files from Google Drive to a local directory.

This script is designed to be run periodically (via cron or systemd timer)
to keep a local media directory in sync with a Google Drive folder.
"""

import argparse
import fcntl
import logging
import logging.handlers
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except ImportError as exc:
    missing = (
        "Missing Google API libraries. Install them with:\n"
        "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
    )
    raise SystemExit(missing) from exc

try:
    import gspread
except ImportError:
    gspread = None


# --- Configuration ---------------------------------------------------------

# Load .env file if available
if load_dotenv:
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)

SCOPES = ["https://www.googleapis.com/auth/drive"]
DEFAULT_FOLDER_ID = "REPLACE_ME_WITH_FOLDER_ID"
DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
DEFAULT_CREDENTIALS_PATH = Path(__file__).resolve().parent / "credentials.json"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_MAX_BYTES = 1_000_000  # rotate sync.log at ~1 MB ...
LOG_BACKUP_COUNT = 5  # ... and keep 5 old files (~6 MB total)
API_RETRIES = 3  # retries for transient Google API / network errors
LOCK_FILE = Path(tempfile.gettempdir()) / "gdrive_sync.lock"


@dataclass
class DriveFile:
    """Minimal metadata we care about for syncing."""

    file_id: str
    name: str
    modified_time: datetime
    mime_type: str
    md5_checksum: Optional[str]

    @property
    def extension(self) -> str:
        return Path(self.name).suffix.lower()


def configure_logging(verbose: bool, log_dir: Optional[Path] = None) -> None:
    """Log to stderr (the systemd journal) and to a size-capped rotating file.

    Older versions rewrote the whole log file on every run to trim old lines,
    which wrote gigabytes a day to the SD card. Rotation only appends.
    """
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers: List[logging.Handler] = [logging.StreamHandler()]

    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_dir / "sync.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
                )
            )
        except OSError as exc:
            print(f"Cannot write logs to {log_dir}: {exc}")

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    # Silence googleapiclient's noisy discovery-cache warnings.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def acquire_lock():
    """Prevent two syncs (e.g. a leftover cron job and the timer) from overlapping."""
    handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def load_drive_service(credentials_path: Path) -> "build":
    creds = service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_drive_files(service, folder_id: str) -> List[DriveFile]:
    query = f"'{folder_id}' in parents and trashed = false"
    fields = (
        "nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum)"
    )
    files: List[DriveFile] = []
    page_token: Optional[str] = None

    while True:
        response = (
            service.files()
            .list(
                q=query,
                pageSize=1000,
                fields=fields,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=API_RETRIES)
        )
        for item in response.get("files", []):
            modified_time = datetime.fromisoformat(
                item["modifiedTime"].replace("Z", "+00:00")
            )
            files.append(
                DriveFile(
                    file_id=item["id"],
                    name=item["name"],
                    mime_type=item["mimeType"],
                    modified_time=modified_time,
                    md5_checksum=item.get("md5Checksum"),
                )
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def _local_file_matches(
    path: Path, entry: DriveFile, file_modified_times: dict[str, float]
) -> bool:
    if not path.exists():
        return False

    local_mtime = file_modified_times.get(path.name)
    if local_mtime is None:
        local_mtime = path.stat().st_mtime
        file_modified_times[path.name] = local_mtime

    remote_ts = entry.modified_time.timestamp()
    # Allow 1-second drift to avoid redundant downloads due to rounding.
    if abs(local_mtime - remote_ts) > 1:
        return False

    return True


def download_file(service, entry: DriveFile, destination: Path) -> None:
    """Download into a hidden temp file, then rename into place.

    The player ignores hidden files, so it never shows a half-downloaded file,
    and a failed download never leaves a truncated file behind.
    """
    logging.info("Downloading %s", entry.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    request = service.files().get_media(fileId=entry.file_id)
    try:
        with open(partial, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=API_RETRIES)
            fh.flush()
            os.fsync(fh.fileno())
        # Preserve the server modified timestamp so we can skip re-download next time.
        os.utime(partial, (time.time(), entry.modified_time.timestamp()))
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()


def log_to_spreadsheet(
    credentials_path: Path,
    spreadsheet_id: str,
    downloaded: int,
    skipped: int,
    deleted: int,
    total: int,
    errors: int = 0
) -> bool:
    """Log sync results to a Google Spreadsheet. Returns True if successful."""
    if not gspread:
        logging.warning("gspread library not installed. Install with: pip install gspread")
        return False

    try:
        # Authenticate with gspread
        gc = gspread.service_account(filename=str(credentials_path))

        # Open the spreadsheet
        try:
            spreadsheet = gc.open_by_key(spreadsheet_id)
        except gspread.exceptions.SpreadsheetNotFound:
            logging.error("Spreadsheet not found. Make sure it's shared with the service account.")
            return False

        # Get or create the worksheet
        try:
            worksheet = spreadsheet.worksheet("Sync Log")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="Sync Log", rows=1000, cols=8)
            # Add headers
            worksheet.append_row([
                "Timestamp",
                "Downloaded",
                "Skipped",
                "Deleted",
                "Total Synced",
                "Errors",
                "Status",
                "Host",
            ])

        # Add the log entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "Success" if errors == 0 else f"Completed with {errors} error(s)"

        worksheet.append_row([
            timestamp,
            downloaded,
            skipped,
            deleted,
            total,
            errors,
            status,
            socket.gethostname(),
        ])

        logging.info("Logged sync results to Google Spreadsheet")
        return True

    except Exception as e:
        logging.error("Failed to log to spreadsheet: %s", e)
        return False


def sync_drive_folder(
    service, folder_id: str, media_dir: Path, allow_empty: bool = False
) -> tuple[List[Path], int, int, int, int]:
    """Mirror the Drive folder into media_dir.

    Returns (synced_paths, downloaded, skipped, deleted, errors).
    """
    logging.info("SYNC STARTED - Drive folder %s -> %s", folder_id, media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    file_metadata: dict[str, float] = {}
    try:
        remote_files = list_drive_files(service, folder_id)
    except HttpError as error:
        logging.error("Google Drive API error: %s", error)
        return [], 0, 0, 0, 1

    # Only plain files can be downloaded: skip subfolders and native Google
    # Docs/Sheets/Slides (mime types starting with application/vnd.google-apps).
    downloadable = [e for e in remote_files if not e.mime_type.startswith("application/vnd.google-apps.")]
    for entry in remote_files:
        if entry.mime_type.startswith("application/vnd.google-apps."):
            logging.debug("Skipping non-downloadable item: %s (%s)", entry.name, entry.mime_type)
    logging.info("Found %d downloadable files in Drive folder", len(downloadable))
    remote_file_names = {entry.name for entry in downloadable}

    synced_paths: List[Path] = []
    downloaded_count = skipped_count = deleted_count = error_count = 0

    for entry in downloadable:
        target_path = media_dir / entry.name
        if _local_file_matches(target_path, entry, file_metadata):
            skipped_count += 1
            logging.debug("Skipped (up-to-date): %s", entry.name)
        else:
            try:
                download_file(service, entry, target_path)
                downloaded_count += 1
                logging.info("Downloaded: %s", entry.name)
            except (HttpError, OSError) as error:
                error_count += 1
                logging.error("Failed to download %s: %s", entry.name, error)
                continue
        synced_paths.append(target_path)

    # Delete local files that no longer exist on Google Drive. If Drive returns
    # nothing at all (e.g. folder sharing was removed from the service account,
    # which shows up as an empty listing rather than an error), keep what we
    # have so the screens don't go blank. Set ALLOW_EMPTY_SYNC=true to allow it.
    local_files = [p for p in media_dir.iterdir() if p.is_file()]
    if not remote_file_names and local_files and not allow_empty:
        logging.warning(
            "Drive folder looks empty but %d local files exist; NOT deleting them. "
            "Check the folder is still shared with the service account, or set ALLOW_EMPTY_SYNC=true.",
            len(local_files),
        )
        error_count += 1
    else:
        for local_file in local_files:
            if local_file.name not in remote_file_names:
                try:
                    local_file.unlink()
                    deleted_count += 1
                    logging.info("Deleted (removed from Drive): %s", local_file.name)
                except OSError as error:
                    error_count += 1
                    logging.error("Failed to delete %s: %s", local_file.name, error)

    logging.info(
        "SYNC SUMMARY: downloaded=%d skipped=%d deleted=%d total=%d errors=%d",
        downloaded_count, skipped_count, deleted_count, len(synced_paths), error_count,
    )
    return synced_paths, downloaded_count, skipped_count, deleted_count, error_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a Google Drive folder to local storage."
    )
    parser.add_argument(
        "--folder-id",
        default=os.environ.get("GDRIVE_FOLDER_ID", DEFAULT_FOLDER_ID),
        help="Google Drive folder ID containing the media files.",
    )
    parser.add_argument(
        "--log-spreadsheet-id",
        default=os.environ.get("LOG_SPREADSHEET_ID", ""),
        help="Google Spreadsheet ID for logging sync results.",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.environ.get("MEDIA_DIR", DEFAULT_MEDIA_DIR)),
        help="Local directory where media files are stored.",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(os.environ.get("CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH)),
        help="Path to the service-account credentials JSON file.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get("LOG_DIR", DEFAULT_LOG_DIR)),
        help="Directory where log files are stored.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        default=os.environ.get("ALLOW_EMPTY_SYNC", "").lower() in ("1", "true", "yes"),
        help="Allow deleting all local media when the Drive folder appears empty.",
    )
    return parser.parse_args()


def validate_config(folder_id: str, credentials_path: Path) -> None:
    if not folder_id or folder_id == DEFAULT_FOLDER_ID:
        raise SystemExit(
            "Drive folder ID is not set. Use --folder-id or set GDRIVE_FOLDER_ID."
        )
    if not credentials_path.exists():
        raise SystemExit(f"Credentials file not found: {credentials_path}")


def main() -> None:
    args = parse_args()
    validate_config(args.folder_id, args.credentials)
    configure_logging(args.verbose, args.log_dir)

    lock = acquire_lock()
    if lock is None:
        logging.info("Another sync is already running; skipping this run.")
        return

    service = load_drive_service(args.credentials)
    synced_paths, downloaded, skipped, deleted, errors = sync_drive_folder(
        service, args.folder_id, args.media_dir, allow_empty=args.allow_empty
    )
    logging.info("Sync complete: %d files in %s", len(synced_paths), args.media_dir)

    if args.log_spreadsheet_id:
        log_to_spreadsheet(
            args.credentials,
            args.log_spreadsheet_id,
            downloaded,
            skipped,
            deleted,
            len(synced_paths),
            errors=errors,
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error: %s", exc)
        raise SystemExit(1) from exc
