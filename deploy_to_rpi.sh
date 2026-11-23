#!/usr/bin/env bash
# Copy this project to the Pi and (optionally) run setup there.
#
#   ./deploy_to_rpi.sh            # copy code + credentials only
#   ./deploy_to_rpi.sh --setup    # copy, then run setup_pi.sh on the Pi
#   ./deploy_to_rpi.sh --diagnose # run diagnose_pi.sh on the Pi
#
# Reads RPI_USER / RPI_HOST / RPI_DIR, GDRIVE_FOLDER_ID, LOG_SPREADSHEET_ID and
# CREDENTIALS_PATH from .env. Uses normal SSH: add your key once with
# `ssh-copy-id USER@HOST` to avoid password prompts (otherwise you are asked
# once per run).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

env_value() { [[ -f .env ]] && sed -n "s/^$1=//p" .env | tail -n1 || true; }

RPI_USER="${RPI_USER:-$(env_value RPI_USER)}"
RPI_HOST="${RPI_HOST:-$(env_value RPI_HOST)}"
RPI_USER="${RPI_USER:-pi}"
RPI_DIR="${RPI_DIR:-$(env_value RPI_DIR)}"
RPI_DIR="${RPI_DIR:-/home/${RPI_USER}/gdrive2video}"
CREDENTIALS="$(env_value CREDENTIALS_PATH)"
CREDENTIALS="${CREDENTIALS:-credentials.json}"
FOLDER_ID="$(env_value GDRIVE_FOLDER_ID)"
SPREADSHEET_ID="$(env_value LOG_SPREADSHEET_ID)"

MODE="${1:-copy}"
[[ -n "${RPI_HOST}" ]] || { echo "Set RPI_HOST in .env (the Pi's IP or hostname, e.g. rpi-video.local)." >&2; exit 1; }
[[ -f "${CREDENTIALS}" ]] || { echo "Credentials file not found: ${CREDENTIALS}" >&2; exit 1; }

# Share one SSH connection for all steps, so a password is asked only once.
CONTROL="${TMPDIR:-/tmp}/gdrive2video-ssh-%C"
SSH=(ssh -o ControlMaster=auto -o "ControlPath=${CONTROL}" -o ControlPersist=120 -o ConnectTimeout=10 "${RPI_USER}@${RPI_HOST}")

echo "=== Target: ${RPI_USER}@${RPI_HOST}:${RPI_DIR} ==="

if [[ "${MODE}" == "--diagnose" ]]; then
  "${SSH[@]}" -t "bash ${RPI_DIR}/diagnose_pi.sh"
  exit
fi

echo "Copying project files (tracked by git)..."
git ls-files -z | COPYFILE_DISABLE=1 tar --null -T - -czf - | "${SSH[@]}" "mkdir -p '${RPI_DIR}' && tar xzf - -C '${RPI_DIR}'"

echo "Copying credentials..."
"${SSH[@]}" "umask 077 && cat > '${RPI_DIR}/credentials.json'" < "${CREDENTIALS}"

if [[ "${MODE}" == "--setup" ]]; then
  echo "Running setup on the Pi (this can take a while on first run)..."
  args=()
  [[ -n "${FOLDER_ID}" ]] && args+=(--folder-id "${FOLDER_ID}")
  [[ -n "${SPREADSHEET_ID}" ]] && args+=(--spreadsheet-id "${SPREADSHEET_ID}")
  "${SSH[@]}" -t "cd '${RPI_DIR}' && ./setup_pi.sh ${args[*]:-}"
else
  echo "Restarting services (if installed)..."
  "${SSH[@]}" "sudo systemctl daemon-reload; sudo systemctl try-restart media-player.service" || true
fi

echo "=== Done ==="
