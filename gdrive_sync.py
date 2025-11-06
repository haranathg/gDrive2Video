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
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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


# --- Configuration ---------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DEFAULT_FOLDER_ID = "REPLACE_ME_WITH_FOLDER_ID"
DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
DEFAULT_CREDENTIALS_PATH = Path(__file__).resolve().parent / "credentials.json"


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


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


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


def sync_drive_folder(service, folder_id: str, media_dir: Path) -> List[Path]:
    logging.info("Syncing Drive folder %s -> %s", folder_id, media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    file_metadata = {}
    try:
        remote_files = list_drive_files(service, folder_id)
    except HttpError as error:
        logging.error("Google Drive API error: %s", error)
        return []

    synced_paths: List[Path] = []
    for entry in remote_files:
        if entry.mime_type == "application/vnd.google-apps.folder":
            continue  # Skip nested folders for now.

        target_path = media_dir / entry.name
        if not _local_file_matches(target_path, entry, file_metadata):
            try:
                download_file(service, entry, target_path)
            except HttpError as error:
                logging.error("Failed to download %s: %s", entry.name, error)
                continue

        synced_paths.append(target_path)

    return synced_paths


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
    configure_logging(args.verbose)
    validate_config(args.folder_id, args.credentials)

    service = load_drive_service(args.credentials)
    logging.info("Starting sync from Google Drive folder %s", args.folder_id)

    synced = sync_drive_folder(service, args.folder_id, args.media_dir)
    if synced:
        logging.info("Successfully synced %d files to %s", len(synced), args.media_dir)
    else:
        logging.warning("No files were synced.")

    logging.info("Sync complete.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error: %s", exc)
        raise SystemExit(1) from exc
