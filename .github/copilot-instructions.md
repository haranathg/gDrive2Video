## Quick context

This repository syncs a Google Drive folder to a Raspberry Pi and plays images/videos as a fullscreen loop. The main entry point is `gdrive2video.py`. The project is designed to run as a systemd service on a Pi (see `gdrive2video.service`) or manually for local testing.

## Key files you should know
- `gdrive2video.py` — main program: sync loop, playback orchestration, CLI flags and environment variable fallbacks.
- `setup_pi.sh` — helper to install system deps (`omxplayer`, `feh`, `ffmpeg`) and Python libs, installs systemd unit and writes `/etc/gdrive2video.env`.
- `gdrive2video.service` — systemd unit. It sources `/etc/gdrive2video.env` and runs `%h/gdrive2video/gdrive2video.py` as user `pi`.
- `README.md` — deployment and troubleshooting steps; use it as canonical user-facing docs.
- `test_gdrive_access.py` — minimal script showing how the service-account auth and Drive API queries are used; useful for reproducing API/auth issues locally.

## What matters for edits or new features
- Drive integration: use the `service_account` credentials and `googleapiclient` (see `load_drive_service` and `list_drive_files`). Mirror the `DriveFile` dataclass when working with file metadata.
- Playback: images -> `feh` (`play_images`), videos -> `omxplayer` (`play_videos`). When adding new media handling, update `IMAGE_EXTENSIONS` / `VIDEO_EXTENSIONS` and `categorize_media_files`.
- Probing: `probe_video_duration` uses `ffprobe`; treat ffprobe as optional (function returns None and code falls back to `DEFAULT_VIDEO_TIMEOUT`).
- Robustness patterns: prefer logging via `configure_logging`, catch `HttpError` around Drive calls, and handle missing system binaries with clear logs (see `run_command`, `play_videos`). Follow these patterns for new CLI or service interactions.

## Important environment & CLI mappings
- Environment variables used by the systemd service or `.env`:
  - `GDRIVE_FOLDER_ID` — required Drive folder ID.
  - `MEDIA_DIR` — local cache directory (service setup uses `/home/pi/media`).
  - `SLIDESHOW_DELAY` — seconds per image.
  - `SYNC_INTERVAL_SECONDS` — poll interval (default 300).
  - `CREDENTIALS_PATH` — path to service-account JSON (script default is `credentials.json` next to the script).
- CLI is equivalent to env vars; examples developers use:
  - Run once, verbose: `python3 gdrive2video.py --folder-id <ID> --once --verbose` (handy for iterative debugging).
  - Local test of Drive access: `python3 test_gdrive_access.py` (edit the `SERVICE_ACCOUNT_FILE` variable in that file to point at the key you want to test).

## System/service debugging shortcuts
- Check service status and logs (common first step):
  - `sudo systemctl status gdrive2video.service`
  - `journalctl -u gdrive2video.service -f`
- If Python imports fail, the `ImportError` message in `gdrive2video.py` already prints the pip install command:
  - `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib`

## Project-specific conventions and gotchas
- Default media cache differs by context:
  - Local/dev default: `./media/` inside the repo (used when running locally).
  - Pi/service default: `/home/pi/media/` (created by `setup_pi.sh` and used by the systemd unit).
- Credentials filename is not uniform in the repo. README and `gdrive2video.py` expect `credentials.json` by default; `test_gdrive_access.py` uses `gdrive2video-access-key.json`. When debugging, ensure you point `CREDENTIALS_PATH` (env) or `--credentials` (CLI) to the correct file.
- The sync currently skips nested folders and does not delete local files removed from Drive. If you change sync semantics, update `sync_drive_folder` and document behavior in `README.md`.

## When you open a PR
- Reference the files you changed and include a short manual test plan: how to run the script once (`--once --verbose`), which service commands to restart, and which log lines validate success (e.g. "Syncing Drive folder" and "Starting image slideshow").
- For changes that touch systemd or `setup_pi.sh`, list exact commands you ran on a Pi (or state that you tested locally with `--media-dir ./media --once`).

## Quick examples (copyable)
- Run single sync+play locally:
  python3 gdrive2video.py --folder-id YOUR_ID --once --verbose
- Tail the service logs on the Pi:
  sudo journalctl -u gdrive2video.service -f

---
If anything here is unclear or you'd like the instructions to include extra examples (unit tests, a dev Dockerfile, or CI guidance), tell me which area to expand. I can iterate on this file.
