#!/usr/bin/env python3
"""
Display and loop through media files in a local directory.

This script continuously plays images as a slideshow and videos in a loop,
making it ideal for digital signage or display purposes on a Raspberry Pi.
"""

import argparse
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Sequence

# --- Configuration ---------------------------------------------------------

DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
SLIDESHOW_DELAY = 8  # seconds
DEFAULT_VIDEO_TIMEOUT = 300  # seconds if video duration probing fails

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

FEH_BASE_CMD = [
    "feh",
    "--fullscreen",
    "--hide-pointer",
    "--auto-zoom",
    "--quiet",
    "--no-fehbg",
]

# Alternative: Use fbi (framebuffer image viewer) for true headless operation
FBI_BASE_CMD = [
    "fbi",
    "--noverbose",
    "--autozoom",
]

VLC_BASE_CMD = [
    "cvlc",  # Command-line VLC
    "--fullscreen",
    "--no-video-title-show",
    "--play-and-exit",
    "--quiet",
]


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


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


def probe_video_duration(video_path: Path) -> Optional[float]:
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
    except subprocess.CalledProcessError as exc:
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


def play_images(image_files: Sequence[Path], delay: int, use_framebuffer: bool = False) -> None:
    if not image_files:
        logging.info("No images found to display.")
        return

    if use_framebuffer:
        # Use fbi for framebuffer display (true headless)
        command = [
            *FBI_BASE_CMD,
            "--timeout",
            str(delay),
            *(str(p) for p in image_files),
        ]
        logging.info("Starting framebuffer slideshow with %d images.", len(image_files))
    else:
        # Use feh (requires X server)
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
        command = [*VLC_BASE_CMD, str(video)]
        logging.info("Playing video: %s", video.name)
        try:
            # VLC will play and exit automatically with --play-and-exit
            subprocess.run(command, check=True, timeout=DEFAULT_VIDEO_TIMEOUT)
        except FileNotFoundError:
            logging.error("VLC is not installed or not in PATH. Install with: sudo apt-get install vlc")
            return
        except subprocess.TimeoutExpired:
            logging.warning("Video playback timed out for %s after %s seconds.", video.name, DEFAULT_VIDEO_TIMEOUT)
        except subprocess.CalledProcessError as exc:
            logging.error("Video playback failed for %s: %s", video, exc)


def playback_loop(media_dir: Path, delay: int, loop: bool = True, use_framebuffer: bool = False) -> None:
    """Main playback loop that cycles through images and videos."""
    stop_requested = False

    def _handle_signal(signum, frame):
        nonlocal stop_requested
        logging.info("Received signal %s, shutting down after current cycle.", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while True:
        images, videos = categorize_media_files(media_dir)
        if not images and not videos:
            logging.warning("No media files available in %s", media_dir)
            if not loop:
                break
            logging.info("Waiting 30 seconds before checking again...")
            time.sleep(30)
            if stop_requested:
                break
            continue

        play_images(images, delay, use_framebuffer)
        if stop_requested:
            break

        play_videos(videos)
        if stop_requested or not loop:
            break

        logging.info("Completed one cycle. Starting over...")

    logging.info("Playback stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play media files from a local directory in a continuous loop."
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path(os.environ.get("MEDIA_DIR", DEFAULT_MEDIA_DIR)),
        help="Local directory where media files are stored.",
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
        help="Play through the media once and exit (no loop).",
    )
    parser.add_argument(
        "--framebuffer",
        action="store_true",
        help="Use framebuffer (fbi) for images instead of feh (for true headless operation).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def validate_config(media_dir: Path) -> None:
    if not media_dir.exists():
        logging.warning("Media directory does not exist: %s", media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        logging.info("Created media directory: %s", media_dir)


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    validate_config(args.media_dir)

    logging.info("Media player started. Playing files from: %s", args.media_dir)
    if args.framebuffer:
        logging.info("Using framebuffer mode (fbi) for images")
    playback_loop(args.media_dir, args.slideshow_delay, loop=not args.once, use_framebuffer=args.framebuffer)
    logging.info("Media player exiting.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error: %s", exc)
        raise SystemExit(1) from exc
