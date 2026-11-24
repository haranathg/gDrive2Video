# gdrive2video

Turns a Raspberry Pi plugged into a TV into a digital sign. It shows the images and videos from a Google Drive folder, fullscreen, on a loop. Drop files into the Drive folder and they appear on the TVs within about 5 minutes. Delete them from Drive and they disappear from the TVs.

```
Google Drive folder ──(every 5 min)──▶ gdrive_sync.py ──▶ media/ ──▶ media_player.py ──▶ mpv ──▶ HDMI
```

| File | What it does |
|---|---|
| `gdrive_sync.py` | Mirrors the Drive folder into the local `media/` folder. It's run every 5 minutes by `gdrive-sync.timer`. It can also log each run to a Google Sheet. |
| `media_player.py` | Keeps one fullscreen `mpv` running, looping all images (8 s each) and then all videos. It reloads when `media/` changes. |
| `setup_pi.sh` | Run **on the Pi**. It installs or updates everything and applies the reliability settings below. It's safe to re-run. |
| `update_pi.sh` | Run **on the Pi** when you want to update. It pulls the latest code from GitHub and applies it. |
| `deploy_to_rpi.sh` | Run **on your Mac**. It copies the project and credentials to the Pi, and with `--setup` it also runs setup there. |
| `diagnose_pi.sh` | Health check on the Pi: services, logs, display, power and an SD-card read test. |
| `*.service`, `*.timer` | systemd unit templates. `setup_pi.sh` fills in the user and path. |

See [docs/DEVICE_NOTES.md](docs/DEVICE_NOTES.md) for the deployed device, open items and incident history.

**There's no desktop.** The Pi boots straight to the player, and mpv draws directly to the screen through DRM/KMS. An earlier version ran on the LXDE desktop, so when the desktop's X server broke, the TVs just showed the desktop.

## Setting up a Pi (new SD card)

1. **Flash the card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
   - OS: **Raspberry Pi OS Lite**. Use the 32-bit version for a Pi 2 and 64-bit for a Pi 3/4/5.
   - Click *Edit settings* and set these:
     - hostname `rpi-video`
     - username and password (for example `srisai`)
     - Wi-Fi network and country
     - timezone
   - Under *Services*, enable SSH and paste your Mac's public key (`cat ~/.ssh/id_ed25519.pub`).
   - Use a "High Endurance" card (SanDisk High Endurance or Samsung PRO Endurance). Signage runs 24/7 and wears out ordinary cards.
2. **Boot the Pi** on the network and wait about 2 minutes. Check that `ping rpi-video.local` gets a reply.
3. **On the Mac**, in this folder:
   - Make sure `.env` has `RPI_USER`, `RPI_HOST=rpi-video.local`, `GDRIVE_FOLDER_ID`, `LOG_SPREADSHEET_ID` and `CREDENTIALS_PATH`. See `.env.sample`.
   - Run:
   ```bash
   ./deploy_to_rpi.sh --setup
   ```
   This takes 10–30 minutes the first time on a Pi 2 (OS updates and Python packages).
4. Reboot when it asks (`ssh rpi-video.local sudo reboot`). The TV should show the slideshow about 1 minute after boot.

### Updating the code later
Push your changes to `main` on GitHub. Then run the update on each Pi whenever you're ready. Nothing updates automatically.
```bash
ssh srisai@<pi-address> ~/gdrive2video/update_pi.sh   # from any computer on the Pi's network
```
The update script:
- pulls the latest `main`
- refuses to switch if the new code fails basic checks, so the screens keep the current version
- re-applies the services and settings, then restarts the player

`update_pi.sh BRANCH` tests another branch, and `--force` re-applies even if nothing changed. The Pi's own files are never touched: credentials, media, logs and `/etc/gdrive2video.env`.

From a Mac on the same network, `./deploy_to_rpi.sh` still works too. It copies the code from your local checkout instead of GitHub.

### What `setup_pi.sh` configures
- OS patches (`apt full-upgrade`) and automatic Debian security updates (`unattended-upgrades`).
- `mpv`, plus a Python virtualenv (`.venv`) with the packages in `requirements.txt`.
- `/etc/gdrive2video.env`: the folder ID, Sheet ID, credentials path and media folder.
- The services `media-player.service` (always restarts) and `gdrive-sync.timer`. It also removes old cron sync entries.
- Boots to the console with no desktop and no login prompt on the TV, and stops the screen from blanking.
- Forces HDMI on at 1920×1080, so the picture appears even if the TV was off or in standby when the Pi booted. To change the resolution, use `--hdmi-mode 1280x720@60`. To turn this off, use `--no-force-hdmi`.
- **Hardware watchdog**: if the Pi ever hangs, it reboots itself after about 15 seconds.
- **Less SD-card wear**: the journal is capped at 50 MB, the swap file is off, sync logs rotate at 1 MB, and the player's temp files live in RAM.

## Day-to-day

| Task | Command (on the Pi) |
|---|---|
| Update to latest code | `~/gdrive2video/update_pi.sh` |
| Health check | `~/gdrive2video/diagnose_pi.sh`, or from the Mac `./deploy_to_rpi.sh --diagnose` |
| Player status and logs | `systemctl status media-player` / `journalctl -u media-player -f` |
| Sync logs | `journalctl -u gdrive-sync -f`, or `logs/sync.log` |
| Sync now | `sudo systemctl start gdrive-sync` |
| Restart player | `sudo systemctl restart media-player` |
| Change settings | edit `/etc/gdrive2video.env`, then `sudo systemctl restart media-player` |

Settings in `/etc/gdrive2video.env`:

| Setting | Meaning |
|---|---|
| `GDRIVE_FOLDER_ID` | The Drive folder to mirror. |
| `LOG_SPREADSHEET_ID` | Optional Google Sheet for sync logs. It gets one row per sync, including the Pi's hostname. |
| `SLIDESHOW_DELAY` | Seconds each image stays on screen. |
| `MEDIA_DIR`, `LOG_DIR`, `CREDENTIALS_PATH` | Local paths. |
| `ALLOW_EMPTY_SYNC=true` | Lets an empty Drive folder clear the screens. By default, if Drive shows the folder as empty, the Pi keeps its current files. That usually means the folder stopped being shared with the service account. |
| `MPV_VO`, `MPV_HWDEC`, `MPV_EXTRA_ARGS` | mpv tuning. For example `MPV_EXTRA_ARGS=--volume=50`, or `--no-audio` to mute videos. |

## Troubleshooting

Start with `diagnose_pi.sh`, then check these:

- **The TV shows the desktop or a login prompt.** The player isn't holding the screen. Run `./setup_pi.sh --no-upgrade` and reboot.
- **The TV shows "no signal" or the wrong resolution.** Turn the TV on and set it to the right input. If it still shows nothing, re-run setup with a lower resolution, for example `--hdmi-mode 1280x720@60`, and reboot.
- **mpv logs "Can't open TTY for VT control" or "dumb mode".** Both are harmless on a Pi 2.
- **Things crash randomly** (Python or X segfaults, garbled config files). Check the *SD card health* section of `diagnose_pi.sh`. If reads of the same file differ, the SD card (or the card slot) is failing, and the only fix is a new card. This happened in Nov 2026. On a Pi 2, also suspect the Pi itself.
- **`throttled=0x50005` (or anything other than `0x0`).** The power supply is too weak. Use the official 2.5 A / 3 A supply. Under-voltage also corrupts SD cards.
- **New files don't appear.** Check that the Drive folder is still shared with the service account email (in `credentials.json`, `client_email`). Then check `journalctl -u gdrive-sync`.
- **Videos stutter on a Pi 2.** Re-encode them to H.264 at 1080p or lower (or 720p) and 30 fps. A Pi 3B+ or 4 handles video much better.

## Local testing (Mac)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python gdrive_sync.py --media-dir /tmp/media --log-spreadsheet-id ""   # reads .env
brew install mpv && MPV_VO=gpu python3 media_player.py --media-dir /tmp/media
```

## Google Drive access (one-time)

1. In Google Cloud Console, enable the **Google Drive API** (and the **Sheets API** if you use Sheet logging).
2. Create a **service account** and download its JSON key. That key is the credentials file.
3. Share the Drive folder, and the log Sheet if you use one, with the service account's email address.
