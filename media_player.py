#!/usr/bin/env python3
"""
Loop the media in a local folder fullscreen on the attached display.

Designed for a Raspberry Pi running WITHOUT a desktop: mpv draws straight to
the screen through DRM/KMS, so there is no X server or desktop to crash or to
show on the TV. One mpv process stays up and loops the whole playlist; when the
synced folder changes, the playlist is reloaded over mpv's IPC socket. If mpv
dies it is restarted, and if this script dies systemd restarts it.

Only the Python standard library is used, so it runs on the system python3.
"""

import argparse
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# --- Configuration ---------------------------------------------------------

DEFAULT_MEDIA_DIR = Path(__file__).resolve().parent / "media"
DEFAULT_SLIDESHOW_DELAY = 8  # seconds per image
POLL_SECONDS = 15  # how often to look for changes in the media folder

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

# "gpu,drm": try the GPU renderer first (smooth video), then fall back to plain
# DRM. Both draw directly to the display without X. Override with MPV_VO
# (for example MPV_VO=gpu when testing under a desktop session).
DEFAULT_MPV_VO = "gpu,drm"
DEFAULT_MPV_HWDEC = "auto-safe"

MediaSignature = Tuple[Tuple[str, int, int], ...]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


# --- Media folder ------------------------------------------------------------


def scan_media(media_dir: Path) -> List[Path]:
    """Return playable files: images first, then videos, each sorted by name.

    Hidden files are skipped; gdrive_sync.py downloads into hidden ".part"
    files and renames them when complete, so half-written files never play.
    """
    images: List[Path] = []
    videos: List[Path] = []
    try:
        entries = sorted(media_dir.iterdir())
    except OSError as exc:
        logging.error("Cannot read media directory %s: %s", media_dir, exc)
        return []

    for path in entries:
        if path.name.startswith(".") or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            images.append(path)
        elif suffix in VIDEO_EXTENSIONS:
            videos.append(path)
    return images + videos


def media_signature(files: Sequence[Path]) -> MediaSignature:
    sig = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


# --- Display detection -------------------------------------------------------


def find_connected_display() -> Optional[Tuple[str, str]]:
    """Return (/dev/dri/cardN, connector) for the first connected display.

    On a Pi the display controller is not always card0 (v3d and vc4 both
    register DRM devices), so tell mpv explicitly which card and connector to
    use instead of relying on its default.
    """
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None
    for status_file in sorted(drm.glob("card*-*/status")):
        try:
            if status_file.read_text().strip() != "connected":
                continue
        except OSError:
            continue
        card, _, connector = status_file.parent.name.partition("-")
        return f"/dev/dri/{card}", connector
    return None


# --- mpv control -------------------------------------------------------------


class MpvPlayer:
    def __init__(self, delay: int, runtime_dir: Path) -> None:
        self.delay = delay
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.ipc_path = runtime_dir / "mpv.sock"
        self.playlist_path = runtime_dir / "playlist.m3u"
        self.proc: Optional[subprocess.Popen] = None

    def _write_playlist(self, files: Sequence[Path]) -> None:
        tmp = self.playlist_path.with_suffix(".tmp")
        tmp.write_text("".join(f"{p}\n" for p in files))
        tmp.replace(self.playlist_path)

    def _command(self) -> List[str]:
        vo = os.environ.get("MPV_VO", DEFAULT_MPV_VO)
        cmd = [
            "mpv",
            "--fullscreen",
            "--idle=yes",  # stay up (black screen) even with an empty playlist
            "--force-window=yes",
            "--loop-playlist=inf",
            f"--image-display-duration={self.delay}",
            "--keepaspect=yes",
            "--no-osc",
            "--osd-level=0",
            "--no-input-default-bindings",
            "--input-terminal=no",
            "--cursor-autohide=always",
            "--msg-level=all=warn",
            f"--hwdec={os.environ.get('MPV_HWDEC', DEFAULT_MPV_HWDEC)}",
            f"--input-ipc-server={self.ipc_path}",
            f"--playlist={self.playlist_path}",
        ]
        if vo:
            cmd.append(f"--vo={vo}")
        if vo and ("drm" in vo or "gpu" in vo) and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            cmd.append("--gpu-context=drm")
            display = find_connected_display()
            if display:
                card, connector = display
                cmd += [f"--drm-device={card}", f"--drm-connector={connector}"]
                logging.info("Using display %s on %s", connector, card)
            else:
                logging.warning("No connected display found in /sys/class/drm; letting mpv pick.")
        cmd += shlex.split(os.environ.get("MPV_EXTRA_ARGS", ""))
        return cmd

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, files: Sequence[Path]) -> None:
        self.stop()
        self._write_playlist(files)
        if self.ipc_path.exists():
            self.ipc_path.unlink()
        cmd = self._command()
        logging.info("Starting mpv with %d file(s)", len(files))
        logging.debug("mpv command: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def _send(self, *command: str) -> bool:
        payload = json.dumps({"command": list(command)}) + "\n"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.alive():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(5)
                    sock.connect(str(self.ipc_path))
                    sock.sendall(payload.encode())
                    # mpv may push event lines before the reply; read until the
                    # line that answers our command (it has an "error" field).
                    buffer = ""
                    while True:
                        chunk = sock.recv(4096).decode(errors="replace")
                        if not chunk:
                            return False
                        buffer += chunk
                        *lines, buffer = buffer.split("\n")
                        for line in lines:
                            try:
                                message = json.loads(line)
                            except ValueError:
                                continue
                            if "error" in message and "event" not in message:
                                return message["error"] == "success"
            except OSError:
                time.sleep(0.5)
        return False

    def reload(self, files: Sequence[Path]) -> None:
        self._write_playlist(files)
        if not files:
            ok = self._send("stop")  # clears the playlist, keeps a black screen
        else:
            ok = self._send("loadlist", str(self.playlist_path), "replace")
        if not ok:
            logging.warning("mpv did not accept the new playlist over IPC; restarting it.")
            self.start(files)

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


# --- Main loop ---------------------------------------------------------------


def playback_loop(media_dir: Path, delay: int, runtime_dir: Path) -> None:
    stop_requested = False

    def _handle_signal(signum, _frame):
        nonlocal stop_requested
        logging.info("Received signal %s, stopping.", signum)
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    player = MpvPlayer(delay, runtime_dir)
    current_sig: Optional[MediaSignature] = None
    pending_sig: Optional[MediaSignature] = None

    try:
        while not stop_requested:
            files = scan_media(media_dir)
            sig = media_signature(files)

            if not player.alive():
                if player.proc is not None:
                    logging.warning("mpv exited with code %s; restarting.", player.proc.returncode)
                if not files:
                    logging.warning("No media files in %s yet; showing a blank screen.", media_dir)
                player.start(files)
                current_sig = pending_sig = sig
            elif sig != current_sig:
                # Wait until the folder has stopped changing for one poll so a
                # sync that is still downloading doesn't cause repeated reloads.
                if sig == pending_sig:
                    logging.info("Media folder changed (%d file(s)); reloading playlist.", len(files))
                    player.reload(files)
                    current_sig = sig
                pending_sig = sig

            for _ in range(POLL_SECONDS * 2):
                if stop_requested:
                    break
                time.sleep(0.5)
    finally:
        player.stop()
    logging.info("Playback stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play media files from a local directory fullscreen in a continuous loop."
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
        default=int(os.environ.get("SLIDESHOW_DELAY", DEFAULT_SLIDESHOW_DELAY)),
        help="Seconds each image remains on screen.",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        # systemd's RuntimeDirectory= sets this to a tmpfs path (no SD writes).
        default=Path(os.environ.get("RUNTIME_DIRECTORY") or Path(tempfile.gettempdir()) / "media-player"),
        help="Where to keep the playlist file and mpv control socket.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument("--framebuffer", action="store_true", help=argparse.SUPPRESS)  # legacy, ignored
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    if args.framebuffer:
        logging.info("--framebuffer is no longer needed (mpv draws to the screen directly); ignoring.")
    if not shutil.which("mpv"):
        raise SystemExit("mpv is not installed. Install it with: sudo apt-get install -y mpv")

    args.media_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Media player started. Playing files from: %s", args.media_dir)
    playback_loop(args.media_dir, args.slideshow_delay, args.runtime_dir)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unhandled error: %s", exc)
        raise SystemExit(1) from exc
