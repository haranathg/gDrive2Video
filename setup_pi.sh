#!/usr/bin/env bash
# Set up (or re-set up) a Raspberry Pi as a gdrive2video display.
#
# Run ON THE PI, from the project folder, as the user that should run the
# player (not root). Safe to run again at any time: it patches the OS,
# reinstalls the services and re-applies all settings.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="$(id -un)"
ENV_FILE="/etc/gdrive2video.env"
UNIT_DIR="/etc/systemd/system"

FOLDER_ID=""
SPREADSHEET_ID=""
DO_UPGRADE=true
KEEP_DESKTOP=false

usage() {
  cat <<EOF
Usage: $0 [--folder-id ID] [--spreadsheet-id ID] [--no-upgrade] [--keep-desktop]

  --folder-id ID        Google Drive folder to mirror (saved to ${ENV_FILE}).
  --spreadsheet-id ID   Google Sheet for sync logging (optional).
  --no-upgrade          Skip 'apt full-upgrade' (faster re-runs).
  --keep-desktop        Don't switch the Pi to boot without the desktop.

Expects credentials.json (the Google service-account key) in ${PROJECT_DIR}.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --folder-id) FOLDER_ID="${2:?missing value}"; shift 2 ;;
    --spreadsheet-id) SPREADSHEET_ID="${2:?missing value}"; shift 2 ;;
    --no-upgrade) DO_UPGRADE=false; shift ;;
    --keep-desktop) KEEP_DESKTOP=true; shift ;;
    --framebuffer) shift ;;  # legacy flag, no longer needed
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "${SERVICE_USER}" == "root" ]]; then
  echo "Run this as the normal Pi user (it uses sudo where needed), not as root." >&2
  exit 1
fi

step() { echo; echo "=== $* ==="; }
REBOOT_NEEDED=false

# Set KEY=VALUE in the env file, replacing an existing line or appending.
set_env() {
  local key="$1" value="$2"
  if sudo grep -q "^${key}=" "${ENV_FILE}"; then
    sudo sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    echo "${key}=${value}" | sudo tee -a "${ENV_FILE}" >/dev/null
  fi
}
set_env_default() {  # only if not already set
  if ! sudo grep -q "^$1=" "${ENV_FILE}"; then set_env "$1" "$2"; fi
}

step "Checking files"
if [[ ! -f "${PROJECT_DIR}/credentials.json" ]]; then
  echo "Missing ${PROJECT_DIR}/credentials.json (Google service-account key)." >&2
  echo "Copy it here first (deploy_to_rpi.sh does this for you)." >&2
  exit 1
fi
chmod 600 "${PROJECT_DIR}/credentials.json"

step "Updating the OS and installing packages"
sudo apt-get update
if ${DO_UPGRADE}; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
  if [[ -f /var/run/reboot-required ]]; then REBOOT_NEEDED=true; fi
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  mpv python3-venv python3-pip ca-certificates unattended-upgrades
sudo apt-get -y autoremove

step "Installing Python packages into ${PROJECT_DIR}/.venv"
[[ -x "${PROJECT_DIR}/.venv/bin/python" ]] || python3 -m venv "${PROJECT_DIR}/.venv"
"${PROJECT_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${PROJECT_DIR}/.venv/bin/pip" install --quiet --upgrade -r "${PROJECT_DIR}/requirements.txt"

step "Writing configuration to ${ENV_FILE}"
sudo touch "${ENV_FILE}"
set_env CREDENTIALS_PATH "${PROJECT_DIR}/credentials.json"
set_env_default MEDIA_DIR "${PROJECT_DIR}/media"
set_env_default LOG_DIR "${PROJECT_DIR}/logs"
set_env_default SLIDESHOW_DELAY 8
if [[ -n "${FOLDER_ID}" ]]; then set_env GDRIVE_FOLDER_ID "${FOLDER_ID}"; fi
if [[ -n "${SPREADSHEET_ID}" ]]; then set_env LOG_SPREADSHEET_ID "${SPREADSHEET_ID}"; fi
sudo chown "root:${SERVICE_USER}" "${ENV_FILE}"
sudo chmod 0640 "${ENV_FILE}"
if ! sudo grep -q "^GDRIVE_FOLDER_ID=." "${ENV_FILE}"; then
  echo "WARNING: GDRIVE_FOLDER_ID is not set. Re-run with --folder-id ID." >&2
fi
mkdir -p "$(sudo sed -n 's/^MEDIA_DIR=//p' "${ENV_FILE}")"

step "Installing systemd services"
for unit in media-player.service gdrive-sync.service gdrive-sync.timer; do
  sed -e "s|@USER@|${SERVICE_USER}|g" -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" \
    "${PROJECT_DIR}/${unit}" | sudo tee "${UNIT_DIR}/${unit}" >/dev/null
  sudo chmod 0644 "${UNIT_DIR}/${unit}"
done
# Older installs also ran the sync from cron; the timer replaces that.
if crontab -l >/dev/null 2>&1; then
  crontab -l | grep -v 'gdrive_sync.py' | crontab - || true
fi

if ! ${KEEP_DESKTOP}; then
  step "Booting straight to the player (no desktop)"
  if [[ "$(systemctl get-default)" != "multi-user.target" ]]; then
    sudo systemctl set-default multi-user.target
    REBOOT_NEEDED=true
  fi
  # No login prompt on the TV; a keyboard can still use Ctrl+Alt+F2.
  sudo systemctl disable getty@tty1.service 2>/dev/null || true

  # Keep the screen from blanking and hide boot text and the cursor.
  CMDLINE=/boot/firmware/cmdline.txt
  [[ -f "${CMDLINE}" ]] || CMDLINE=/boot/cmdline.txt
  if [[ -f "${CMDLINE}" ]]; then
    line="$(head -n1 "${CMDLINE}")"
    new="$(sed 's/console=tty1/console=tty3/' <<<"${line}")"
    for opt in consoleblank=0 vt.global_cursor_default=0 logo.nologo; do
      [[ " ${new} " == *" ${opt} "* ]] || new="${new} ${opt}"
    done
    if [[ "${new}" != "${line}" ]]; then
      sudo cp "${CMDLINE}" "${CMDLINE}.bak"
      echo "${new}" | sudo tee "${CMDLINE}" >/dev/null
      REBOOT_NEEDED=true
    fi
  fi
fi

step "Reliability: hardware watchdog, automatic security updates, less SD wear"
# Reboot automatically if the system ever hangs.
sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' |
  sudo tee /etc/systemd/system.conf.d/gdrive2video-watchdog.conf >/dev/null
# Cap the journal so logs can't fill the card.
printf '[Journal]\nSystemMaxUse=50M\n' |
  sudo tee /etc/systemd/journald.conf.d/gdrive2video.conf >/dev/null
# Install Debian security updates automatically.
echo 'unattended-upgrades unattended-upgrades/enable_auto_updates boolean true' |
  sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive unattended-upgrades
# A swap file on the SD card causes constant writes; the player doesn't need it.
if systemctl list-unit-files dphys-swapfile.service >/dev/null 2>&1; then
  sudo dphys-swapfile swapoff 2>/dev/null || true
  sudo systemctl disable --now dphys-swapfile.service 2>/dev/null || true
fi

step "Starting services"
sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable --now gdrive-sync.timer
sudo systemctl enable media-player.service
sudo systemctl start --no-block gdrive-sync.service
if [[ "$(systemctl get-default)" == "multi-user.target" ]] && ! systemctl is-active --quiet display-manager; then
  sudo systemctl restart media-player.service
fi

echo
echo "=== Setup complete ==="
echo "  Status:   systemctl status media-player gdrive-sync.timer"
echo "  Logs:     journalctl -u media-player -u gdrive-sync -f"
echo "  Health:   ${PROJECT_DIR}/diagnose_pi.sh"
if ${REBOOT_NEEDED}; then
  echo
  echo ">>> Reboot required to finish: sudo reboot"
fi
