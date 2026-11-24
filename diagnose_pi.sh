#!/usr/bin/env bash
# Health check for a gdrive2video display. Run on the Pi:
#   ./diagnose_pi.sh
# or from the Mac:  ./deploy_to_rpi.sh --diagnose
# Read-only apart from dropping the page cache for the SD-card test.

section() { echo; echo "=== $* ==="; }

section "System"
grep PRETTY_NAME /etc/os-release
tr -d '\0' </proc/device-tree/model; echo
uptime
df -h / | tail -1
free -h | sed -n 2p

section "Power / temperature"
# throttled=0x0 is healthy; 0x50000 or 0x50005 means under-voltage (bad power supply).
vcgencmd get_throttled 2>/dev/null || echo "vcgencmd not available"
vcgencmd measure_temp 2>/dev/null || true

section "Services"
systemctl --no-pager --lines=0 status media-player.service gdrive-sync.timer 2>&1 | grep -E "●|Active:|Trigger:"
echo "Boot target: $(systemctl get-default)"
if systemctl is-active --quiet display-manager; then
  echo "WARNING: the desktop is running; it can hold the screen instead of the player."
fi

section "Player log (last 15 lines)"
journalctl -u media-player.service -n 15 --no-pager 2>/dev/null

section "Sync log (last 10 lines)"
journalctl -u gdrive-sync.service -n 10 --no-pager 2>/dev/null

section "Display"
for s in /sys/class/drm/card*-*/status; do echo "$(basename "$(dirname "$s")"): $(cat "$s")"; done
pgrep -a mpv | cut -c1-120 || echo "mpv is NOT running"

section "Media"
MEDIA_DIR="$(sudo sed -n 's/^MEDIA_DIR=//p' /etc/gdrive2video.env 2>/dev/null)"
echo "MEDIA_DIR=${MEDIA_DIR:-?}  files: $(find "${MEDIA_DIR:-/nonexistent}" -maxdepth 1 -type f ! -name '.*' 2>/dev/null | wc -l)"

section "SD card health"
# A failing card (or card slot) returns different data for the same file.
# This is what broke the display in Nov 2026.
sudo dmesg | grep -iE "mmc0.*(error|timeout)|EXT4-fs error|I/O error" | tail -5 || true
f="$(find /usr/lib -type f -size +5M 2>/dev/null | head -1)"
if [[ -n "$f" ]]; then
  sums=()
  for _ in 1 2 3; do
    sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
    sums+=("$(md5sum "$f" | cut -d' ' -f1)")
  done
  if [[ "${sums[0]}" == "${sums[1]}" && "${sums[1]}" == "${sums[2]}" ]]; then
    echo "OK: 3 uncached reads of $(basename "$f") matched."
  else
    echo "FAIL: reads of the same file differ (${sums[*]}). The SD card or card slot is failing; replace the card."
  fi
fi
