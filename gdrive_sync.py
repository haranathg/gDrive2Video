#!/usr/bin/env python3
"""
Sync media files from Google Drive to a local directory.

This script is designed to be run periodically (via cron or systemd timer)
to keep a local media directory in sync with a Google Drive folder.
"""

import argparse
import io
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
DEFAULT_LOG_RETENTION_DAYS = 3
DEFAULT_HISTORY_RETENTION_DAYS = 90


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
    level = logging.DEBUG if verbose else logging.INFO

    handlers = []

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    handlers.append(console_handler)

    # File handlers if log_dir is provided
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

        # Current log (with retention)
        current_log = log_dir / "sync_current.log"
        current_handler = logging.FileHandler(current_log, mode='a')
        current_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        handlers.append(current_handler)

        # Full history log
        history_log = log_dir / "sync_history.log"
        history_handler = logging.FileHandler(history_log, mode='a')
        history_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        handlers.append(history_handler)

    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )


def cleanup_log_file(log_file: Path, retention_days: int, log_name: str) -> None:
    """Remove log entries older than retention_days from a log file."""
    if not log_file.exists():
        return

    cutoff_time = datetime.now() - timedelta(days=retention_days)
    temp_log = log_file.parent / f"{log_file.name}.tmp"

    try:
        with open(log_file, 'r') as infile, open(temp_log, 'w') as outfile:
            for line in infile:
                # Parse the timestamp from the log line
                # Format: "2024-11-24 15:30:45,123 [INFO] message"
                try:
                    timestamp_str = line.split(',')[0]
                    log_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    if log_time >= cutoff_time:
                        outfile.write(line)
                except (ValueError, IndexError):
                    # If we can't parse the timestamp, keep the line
                    outfile.write(line)

        # Replace the old log with the cleaned version
        temp_log.replace(log_file)
        logging.debug("Cleaned up %s, keeping last %d days", log_name, retention_days)
    except Exception as e:
        logging.warning("Failed to cleanup %s: %s", log_name, e)
        if temp_log.exists():
            temp_log.unlink()


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
            .execute()
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
    logging.info("Downloading %s", entry.name)
    request = service.files().get_media(fileId=entry.file_id)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with io.FileIO(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    # Preserve the server modified timestamp so we can skip re-download next time.
    os.utime(destination, (time.time(), entry.modified_time.timestamp()))


def log_to_spreadsheet(
    credentials_path: Path,
    spreadsheet_id: str,
    downloaded: int,
    skipped: int,
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
            worksheet = spreadsheet.add_worksheet(title="Sync Log", rows=1000, cols=6)
            # Add headers
            worksheet.append_row([
                "Timestamp",
                "Downloaded",
                "Skipped",
                "Total Synced",
                "Errors",
                "Status"
            ])

        # Add the log entry
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "Success" if errors == 0 else f"Completed with {errors} error(s)"

        worksheet.append_row([
            timestamp,
            downloaded,
            skipped,
            total,
            errors,
            status
        ])

        logging.info("Logged sync results to Google Spreadsheet")
        return True

    except Exception as e:
        logging.error("Failed to log to spreadsheet: %s", e)
        return False


def sync_drive_folder(service, folder_id: str, media_dir: Path) -> tuple[List[Path], int, int]:
    logging.info("=" * 60)
    logging.info("SYNC STARTED - Drive folder %s -> %s", folder_id, media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    file_metadata = {}
    try:
        remote_files = list_drive_files(service, folder_id)
        logging.info("Found %d files in Drive folder (excluding subfolders)", len(remote_files))
    except HttpError as error:
        logging.error("Google Drive API error: %s", error)
        return [], 0, 0

    synced_paths: List[Path] = []
    downloaded_count = 0
    skipped_count = 0

    for entry in remote_files:
        if entry.mime_type == "application/vnd.google-apps.folder":
            logging.debug("Skipping subfolder: %s", entry.name)
            continue  # Skip nested folders for now.

        target_path = media_dir / entry.name
        if not _local_file_matches(target_path, entry, file_metadata):
            try:
                download_file(service, entry, target_path)
                downloaded_count += 1
                logging.info("✓ Downloaded: %s", entry.name)
            except HttpError as error:
                logging.error("✗ Failed to download %s: %s", entry.name, error)
                continue
        else:
            skipped_count += 1
            logging.debug("⊘ Skipped (up-to-date): %s", entry.name)

        synced_paths.append(target_path)

    logging.info("-" * 60)
    logging.info("SYNC SUMMARY:")
    logging.info("  Downloaded: %d files", downloaded_count)
    logging.info("  Skipped (up-to-date): %d files", skipped_count)
    logging.info("  Total synced: %d files", len(synced_paths))
    logging.info("=" * 60)

    return synced_paths, downloaded_count, skipped_count


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
        "--log-retention-days",
        type=int,
        default=int(os.environ.get("LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS)),
        help="Number of days to keep in the current log file (default: 3).",
    )
    parser.add_argument(
        "--history-retention-days",
        type=int,
        default=int(os.environ.get("HISTORY_RETENTION_DAYS", DEFAULT_HISTORY_RETENTION_DAYS)),
        help="Number of days to keep in the history log file (default: 90).",
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

    # Clean up old entries from logs BEFORE setting up logging
    current_log = args.log_dir / "sync_current.log"
    history_log = args.log_dir / "sync_history.log"
    cleanup_log_file(current_log, args.log_retention_days, "current log")
    cleanup_log_file(history_log, args.history_retention_days, "history log")

    # Now configure logging (appends to cleaned logs)
    configure_logging(args.verbose, args.log_dir)

    service = load_drive_service(args.credentials)
    logging.info("Starting sync from Google Drive folder %s", args.folder_id)

    synced_paths, downloaded, skipped = sync_drive_folder(
        service, args.folder_id, args.media_dir
    )
    if synced_paths:
        logging.info("Successfully synced %d files to %s", len(synced_paths), args.media_dir)
    else:
        logging.warning("No files were synced.")

    logging.info("Sync complete.")

    # Log to Google Spreadsheet if configured
    if args.log_spreadsheet_id:
        log_to_spreadsheet(
            args.credentials,
            args.log_spreadsheet_id,
            downloaded,
            skipped,
            len(synced_paths),
            errors=0  # Could track errors if needed
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error: %s", exc)
        raise SystemExit(1) from exc
