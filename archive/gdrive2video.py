#!/usr/bin/env python3
"""
Continuously sync media from Google Drive and play it on a Raspberry Pi.

This script expects a service account credential JSON (`credentials.json`)
located next to the script. The Google Drive folder ID can be set through the
`GDRIVE_FOLDER_ID` environment variable or by editing the `DEFAULT_FOLDER_ID`
constant below.
"""

import argparse
import io
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except ImportError as exc:  # pragma: no cover - library availability check
    missing = (
        "Missing Google API libraries. Install them with:\n"
        "  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
    )
    raise SystemExit(missing) from exc


# --- Configuration ---------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DEFAULT_FOLDER_ID = "REPLACE_ME_WITH_FOLDER_ID"
DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
SYNC_INTERVAL_SECONDS = 300  # 5 minutes
SLIDESHOW_DELAY = 8  # seconds
DEFAULT_VIDEO_TIMEOUT = 300  # seconds if video duration probing fails
DEFAULT_CREDENTIALS_PATH = Path(__file__).resolve().parent / "credentials.json"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}

FEH_BASE_CMD = [
    "feh",
    "--fullscreen",
    "--hide-pointer",
    "--auto-zoom",
    "--quiet",
]

OMXPLAYER_BASE_CMD = [
    "omxplayer",
    "--no-osd",
    "--loop",
]


@dataclass
class DriveFile:
    """Minimal metadata we care about for syncing."""

    file_id: str
    name: str
    modified_time: datetime
    mime_type: str
    md5_checksum: str | None

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
    page_token: str | None = None

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


def categorize_media_files(media_dir: Path) -> tuple[List[Path], List[Path]]:
    image_files: List[Path] = []
    video_files: List[Path] = []

    for path in sorted(media_dir.glob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            image_files.append(path)
        elif suffix in VIDEO_EXTENSIONS:
            video_files.append(path)
    return image_files, video_files


def probe_video_duration(video_path: Path) -> float | None:
    """Return the video duration in seconds if ffprobe is available."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "-show_entries",
                "format=duration",
                str(video_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logging.debug("ffprobe not installed; falling back to default timeout.")
        return None
    except subprocess.CalledProcessError as exc:  # pragma: no cover - best effort
        logging.warning("Failed to probe duration for %s: %s", video_path, exc)
        return None

    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return None


def run_command(command: Sequence[str]) -> None:
    logging.debug("Executing command: %s", " ".join(command))
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        logging.error("Command not found: %s", command[0])
    except subprocess.CalledProcessError as exc:
        logging.error("Command failed (%s): %s", exc.returncode, exc)


def play_images(image_files: Sequence[Path], delay: int) -> None:
    if not image_files:
        logging.info("No images found to display.")
        return

    command = [
        *FEH_BASE_CMD,
        "--slideshow-delay",
        str(delay),
        "--cycle-once",
        *(str(p) for p in image_files),
    ]
    logging.info("Starting image slideshow with %d images.", len(image_files))
    run_command(command)


def play_videos(video_files: Sequence[Path]) -> None:
    if not video_files:
        logging.info("No videos found to play.")
        return

    for video in video_files:
        command = [*OMXPLAYER_BASE_CMD, str(video)]
        logging.info("Playing video: %s", video.name)
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            timeout = probe_video_duration(video) or DEFAULT_VIDEO_TIMEOUT
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logging.debug(
                    "Stopping looped playback for %s after %s seconds.",
                    video.name,
                    timeout,
                )
        except FileNotFoundError:
            logging.error("omxplayer is not installed or not in PATH.")
            return
        except subprocess.SubprocessError as exc:
            logging.error("Video playback failed for %s: %s", video, exc)
        finally:
            # Ensure the player is stopped before moving on.
            if proc and proc.poll() is None:
                try:
                    if proc.stdin:
                        proc.stdin.write(b"q")
                        proc.stdin.flush()
                    proc.wait(timeout=5)
                except Exception:  # pragma: no cover - best effort cleanup
                    proc.kill()


def playback_loop(media_dir: Path, delay: int) -> None:
    images, videos = categorize_media_files(media_dir)
    if not images and not videos:
        logging.warning("No media files available in %s", media_dir)
        return

    play_images(images, delay)
    play_videos(videos)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a Google Drive folder to local storage and play media."
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
        "--sync-interval",
        type=int,
        default=int(os.environ.get("SYNC_INTERVAL_SECONDS", SYNC_INTERVAL_SECONDS)),
        help="Seconds to wait between sync cycles (default: 300).",
    )
    parser.add_argument(
        "--slideshow-delay",
        type=int,
        default=int(os.environ.get("SLIDESHOW_DELAY", SLIDESHOW_DELAY)),
        help="Seconds each image remains on screen.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync + playback pass and then exit.",
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
    logging.info("gdrive2video started; syncing every %d seconds.", args.sync_interval)

    stop_requested = False

    def _handle_signal(signum, frame):
        nonlocal stop_requested
        logging.info("Received signal %s, shutting down after current cycle.", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while True:
        synced = sync_drive_folder(service, args.folder_id, args.media_dir)
        if synced:
            logging.debug("Synced %d files.", len(synced))
        playback_loop(args.media_dir, args.slideshow_delay)

        if args.once or stop_requested:
            break

        logging.info("Waiting %d seconds before next sync.", args.sync_interval)
        for _ in range(args.sync_interval):
            if stop_requested:
                break
            time.sleep(1)
        if stop_requested:
            break

    logging.info("gdrive2video exiting.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        raise
    except Exception as exc:  # pragma: no cover - last resort logging
        logging.exception("Unhandled error: %s", exc)
        sys.exit(1)
