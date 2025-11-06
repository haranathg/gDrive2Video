# gdrive2video

Play a fullscreen slideshow of images and loop videos from a Google Drive folder on a Raspberry Pi connected over HDMI. The project is split into two separate components:

1. **gdrive_sync.py** - A sync script that downloads media from Google Drive to a local folder (runs periodically via systemd timer)
2. **media_player.py** - A player module that continuously displays and loops through the local media files

## Features
- **Sync script** checks Google Drive folder periodically (default: every 5 minutes) and downloads new/updated files
- **Media player** runs continuously, playing images as a slideshow (8s per slide) and videos in loop mode
- Separation of concerns: sync can run independently from playback
- Ships with systemd units for automatic start on boot

## Architecture

The new architecture separates download and playback:

```
┌─────────────────────┐         ┌──────────────────┐
│  Google Drive       │         │  Raspberry Pi    │
│  ┌──────────────┐   │         │                  │
│  │ Media Folder │───┼────────▶│  gdrive_sync.py  │
│  └──────────────┘   │  Timer  │        ↓         │
└─────────────────────┘  (5min) │   ./media/       │
                                 │        ↓         │
                                 │  media_player.py │
                                 │        ↓         │
                                 │   [Display]      │
                                 └──────────────────┘
```

## 1. Prepare Google Drive Access
1. Visit the [Google Cloud Console](https://console.cloud.google.com/) and create/select a project.
2. Enable the **Google Drive API** (`APIs & Services` → `Enable APIs and Services`).
3. Create a **service account** (`IAM & Admin` → `Service Accounts`) and download its JSON key. Rename the file to `credentials.json`.
4. Share the Drive folder that will contain your media with the service account email address (e.g. `my-service-account@my-project.iam.gserviceaccount.com`) and give it at least Viewer access.

## 2. Deploy to the Raspberry Pi
1. Copy this repository to the Pi:
   ```bash
   scp -r gdrive2video pi@raspberrypi.local:/home/pi/
   ```
2. Copy `credentials.json` into the project directory on the Pi:
   ```bash
   scp credentials.json pi@raspberrypi.local:/home/pi/gdrive2video/
   ```
3. SSH into the Pi and run the setup helper:
   ```bash
   cd /home/pi/gdrive2video
   chmod +x setup_pi.sh
   ./setup_pi.sh --folder-id YOUR_DRIVE_FOLDER_ID
   ```
   The script will:
   - Update apt and install `python3`, `pip`, `omxplayer`, `feh`, and `ffmpeg`
   - Install the required Python libraries (`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`)
   - Create `/home/pi/media/` and ensure the `pi` user owns it
   - Install both systemd services and timer, enable and start them

If you prefer manual setup, create `/etc/gdrive2video.env`:
```bash
sudo tee /etc/gdrive2video.env >/dev/null <<'EOF'
GDRIVE_FOLDER_ID=YOUR_DRIVE_FOLDER_ID
MEDIA_DIR=/home/pi/media
SLIDESHOW_DELAY=8
EOF
```

Then install and enable the services:
```bash
sudo cp gdrive-sync.service gdrive-sync.timer media-player.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gdrive-sync.timer
sudo systemctl enable --now media-player.service
```

For local testing you can also copy the provided `.env` file, populate `GDRIVE_FOLDER_ID`, and point `CREDENTIALS_PATH` at your key file:
```bash
set -a
source .env
set +a
# Test sync
python3 gdrive_sync.py --verbose
# Test player
python3 media_player.py --once --verbose
```

## 3. Service Control

### Sync Service
- Check timer status: `sudo systemctl status gdrive-sync.timer`
- Check last sync: `sudo systemctl status gdrive-sync.service`
- View sync logs: `journalctl -u gdrive-sync.service -f`
- Manually trigger sync: `sudo systemctl start gdrive-sync.service`

### Player Service
- Check status: `sudo systemctl status media-player.service`
- View logs: `journalctl -u media-player.service -f`
- Restart player: `sudo systemctl restart media-player.service`

Edit `/etc/gdrive2video.env` to change configuration, then restart the affected service.

## 4. Running Manually (Optional)

### Sync Script
Run a one-time sync:
```bash
python3 gdrive_sync.py --folder-id YOUR_DRIVE_FOLDER_ID --verbose
```

Useful flags:
- `--media-dir /path/to/dir` - Override the media cache location
- `--credentials /path/to/creds.json` - Use different credentials

### Media Player
Run the player once through the media:
```bash
python3 media_player.py --once --verbose
```

Run the player in continuous loop mode:
```bash
python3 media_player.py --verbose
```

Useful flags:
- `--media-dir /path/to/dir` - Override the media directory
- `--slideshow-delay 5` - Adjust image display time (seconds)
- `--once` - Play through media once and exit (no loop)

## 5. Adjusting Sync Frequency

The default sync interval is 5 minutes. To change this, edit [gdrive-sync.timer](gdrive-sync.timer#L9) and modify the `OnUnitActiveSec` value:

```ini
[Timer]
OnBootSec=1min
OnUnitActiveSec=10min  # Change to desired interval
Persistent=true
```

Then reload and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart gdrive-sync.timer
```

## 6. Troubleshooting
- Ensure the Pi is connected to the internet; the first sync downloads all media
- If the service account cannot see your files, confirm the Drive folder is shared with it
- `omxplayer` requires the HDMI display to be active; ensure the monitor/TV is on before the player service starts
- The player attempts to read video length with `ffprobe`; if playback does not advance, install `ffmpeg` (handled by `setup_pi.sh`)
- Files deleted from Drive are not removed locally; clear them manually from the media directory if needed

## 7. File Structure

```
gdrive2video/
├── gdrive_sync.py           # Sync script (downloads from Drive)
├── media_player.py          # Player module (displays media)
├── gdrive-sync.service      # Systemd service for sync
├── gdrive-sync.timer        # Systemd timer for periodic sync
├── media-player.service     # Systemd service for player
├── setup_pi.sh              # Setup script for Raspberry Pi
├── credentials.json         # Google service account credentials
├── .env                     # Environment variables (local testing)
├── media/                   # Local media cache directory
└── archive/                 # Old combined script (deprecated)
    └── gdrive2video.py
```

Happy streaming! Place images/videos in the shared Drive folder and they will appear on the Pi after the next sync cycle (default: every 5 minutes).

## 8. Possible Future Enhancements

### Raspberry Pi 3+ Optimizations

The current setup works on all Raspberry Pi models (2, 3, 4, 5) without modifications. For better performance on newer models, consider these optional improvements:

#### Hardware Video Acceleration

Enable hardware decoding in VLC for smoother video playback:

```python
# In media_player.py, update VLC_BASE_CMD:
VLC_BASE_CMD = [
    "cvlc",
    "--fullscreen",
    "--no-video-title-show",
    "--play-and-exit",
    "--quiet",
    "--avcodec-hw=any",  # Enable hardware decoding
    "--vout=gles2",      # Use GLES2 for better performance
]
```

#### Alternative: Use mpv for Better Performance

On RPi3+, `mpv` often outperforms VLC:

1. Install mpv: `sudo apt-get install mpv`
2. Update `media_player.py`:

```python
MPV_BASE_CMD = [
    "mpv",
    "--fullscreen",
    "--no-osc",
    "--no-osd-bar",
    "--hwdec=auto",      # Hardware decoding
    "--vo=gpu",          # GPU video output
    "--really-quiet",
]
```

3. Replace `VLC_BASE_CMD` usage with `MPV_BASE_CMD` in the `play_videos()` function

#### Higher Resolution Support

RPi3+ models support 4K displays. The current code automatically adapts to your display resolution, so no changes are needed. Simply connect a 4K monitor and the system will use the full resolution.

### Additional Features to Consider

- **Web interface** for remote management and monitoring
- **Scheduling** to display different content at different times
- **Multi-zone support** for multiple displays
- **Transition effects** between images
- **Remote control** via REST API or mobile app
