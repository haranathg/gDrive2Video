#!/usr/bin/env bash
set -euo pipefail

SYNC_SERVICE="gdrive-sync.service"
SYNC_TIMER="gdrive-sync.timer"
PLAYER_SERVICE="media-player.service"
SERVICE_DEST="/etc/systemd/system"
ENV_FILE="/etc/gdrive2video.env"
SERVICE_USER="${USER:-pi}"
SERVICE_GROUP="${USER:-pi}"
MEDIA_DIR="/home/${SERVICE_USER}/media"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $0 [--folder-id DRIVE_FOLDER_ID] [--framebuffer]

Options:
  --folder-id    Google Drive folder ID to store in ${ENV_FILE}.
  --framebuffer  Use framebuffer (fbi) for images instead of feh (for true headless RPi2).
  -h, --help     Show this help message.

This script must be run on the Raspberry Pi that will play the media. It will:
  * Update apt packages and install required system dependencies.
  * Install Python packages needed by gdrive sync.
  * Create the media directory at ${MEDIA_DIR}.
  * Install and enable both sync timer and player systemd units.
EOF
}

FOLDER_ID=""
USE_FRAMEBUFFER="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --folder-id)
      [[ $# -lt 2 ]] && { echo "Missing value for --folder-id" >&2; usage; exit 1; }
      FOLDER_ID="$2"
      shift 2
      ;;
    --framebuffer)
      USE_FRAMEBUFFER="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

echo "=== Raspberry Pi 2 Media Display Setup ==="
echo "This script is optimized for RPi2 headless operation."
echo ""

echo "Updating apt package cache..."
sudo apt-get update

echo "Installing system dependencies..."
if [[ "${USE_FRAMEBUFFER}" == "true" ]]; then
  echo "  - Installing fbi (framebuffer image viewer) for headless operation"
  sudo apt-get install -y \
    python3 \
    python3-pip \
    vlc \
    fbi \
    ffmpeg
else
  echo "  - Installing feh (requires X server)"
  sudo apt-get install -y \
    python3 \
    python3-pip \
    vlc \
    feh \
    ffmpeg
fi

echo "Upgrading pip and installing Python packages..."
sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install --upgrade \
  google-api-python-client \
  google-auth-httplib2 \
  google-auth-oauthlib

echo "Preparing media directory at ${MEDIA_DIR}..."
sudo mkdir -p "${MEDIA_DIR}"
sudo chown "${SERVICE_USER}:${SERVICE_GROUP}" "${MEDIA_DIR}"

echo "Installing systemd services and timer..."
# Copy and update service files with correct user
sudo cp "${PROJECT_DIR}/${SYNC_SERVICE}" "${SERVICE_DEST}/${SYNC_SERVICE}"
sudo cp "${PROJECT_DIR}/${SYNC_TIMER}" "${SERVICE_DEST}/${SYNC_TIMER}"
sudo cp "${PROJECT_DIR}/${PLAYER_SERVICE}" "${SERVICE_DEST}/${PLAYER_SERVICE}"

# Update User and Group in service files
sudo sed -i "s/User=pi/User=${SERVICE_USER}/" "${SERVICE_DEST}/${SYNC_SERVICE}"
sudo sed -i "s/Group=pi/Group=${SERVICE_GROUP}/" "${SERVICE_DEST}/${SYNC_SERVICE}"
sudo sed -i "s/User=pi/User=${SERVICE_USER}/" "${SERVICE_DEST}/${PLAYER_SERVICE}"
sudo sed -i "s/Group=pi/Group=${SERVICE_GROUP}/" "${SERVICE_DEST}/${PLAYER_SERVICE}"

sudo chmod 0644 "${SERVICE_DEST}/${SYNC_SERVICE}"
sudo chmod 0644 "${SERVICE_DEST}/${SYNC_TIMER}"
sudo chmod 0644 "${SERVICE_DEST}/${PLAYER_SERVICE}"

if [[ -n "${FOLDER_ID}" ]]; then
  echo "Writing environment configuration to ${ENV_FILE}..."
  if [[ "${USE_FRAMEBUFFER}" == "true" ]]; then
    sudo tee "${ENV_FILE}" >/dev/null <<EOF
GDRIVE_FOLDER_ID=${FOLDER_ID}
MEDIA_DIR=${MEDIA_DIR}
SLIDESHOW_DELAY=8
USE_FRAMEBUFFER=true
EOF
  else
    sudo tee "${ENV_FILE}" >/dev/null <<EOF
GDRIVE_FOLDER_ID=${FOLDER_ID}
MEDIA_DIR=${MEDIA_DIR}
SLIDESHOW_DELAY=8
EOF
  fi
  sudo chown root:root "${ENV_FILE}"
  sudo chmod 0644 "${ENV_FILE}"
else
  if [[ ! -f "${ENV_FILE}" ]]; then
    cat <<EOF

Next steps:
  * Create ${ENV_FILE} with at least:
        GDRIVE_FOLDER_ID=YOUR_GOOGLE_DRIVE_FOLDER_ID
        MEDIA_DIR=${MEDIA_DIR}
  * Optionally override SLIDESHOW_DELAY.
  * For framebuffer mode, add: USE_FRAMEBUFFER=true

EOF
  fi
fi

# Update player service to use framebuffer if requested
if [[ "${USE_FRAMEBUFFER}" == "true" ]]; then
  echo "Configuring player to use framebuffer mode..."
  sudo sed -i 's|ExecStart=\(.*\)media_player.py|ExecStart=\1media_player.py --framebuffer|' "${SERVICE_DEST}/${PLAYER_SERVICE}"
fi

echo "Reloading systemd and enabling services..."
sudo systemctl daemon-reload
sudo systemctl enable "${SYNC_TIMER}"
sudo systemctl enable "${PLAYER_SERVICE}"
sudo systemctl start "${SYNC_TIMER}"
sudo systemctl start "${PLAYER_SERVICE}"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Check service status with:"
echo "  sudo systemctl status ${SYNC_TIMER}"
echo "  sudo systemctl status ${PLAYER_SERVICE}"
echo ""
echo "View logs with:"
echo "  journalctl -u ${SYNC_SERVICE} -f"
echo "  journalctl -u ${PLAYER_SERVICE} -f"
echo ""
echo "The sync will run every 5 minutes. First sync starting in 1 minute..."
