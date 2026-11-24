# Device notes

Operating notes for the signage Pi: what's deployed, what went wrong before, and what's still open. For setup and day-to-day commands, see the [README](../README.md).

## Current setup (Nov 2026)

| | |
|---|---|
| Hardware | Raspberry Pi 2 Model B v1.1 (32-bit ARM, 1 GB RAM) |
| Hostname | `rpi-video` (reachable as `rpi-video.local` on the same network) |
| OS | Raspberry Pi OS 13 (Trixie) Lite, 32-bit. No desktop. |
| SD card | 32 GB. It passed a full write-and-read-back test with `f3` before install. |
| Wi-Fi | USB dongle (Realtek RTL8188EUS). The OS has its driver built in; no custom driver needed. The home and site networks are both saved, so it connects in either place. |
| Display | mpv draws straight to HDMI (no desktop). HDMI is forced on at 1920×1080, so the picture appears even if the TV was off at boot. |
| Code | `~/gdrive2video` is a git checkout of this repo on `main`. |
| Config | `/etc/gdrive2video.env` (Drive folder, Sheet, paths) |
| Services | `media-player.service` (always restarts) and `gdrive-sync.timer` (every 5 min) |
| Reliability | Hardware watchdog, automatic security updates, capped logs, no swap file |

### Access
- The Pi usually runs at a remote site, which is reachable only through a Mac on that network. SSH to the Pi from there.
- Updates happen **only when someone runs** `~/gdrive2video/update_pi.sh` on the Pi. This is deliberate, so nobody's change reaches the TVs unexpectedly. Don't add automatic updates.

## Open items
- [ ] **Power supply.** During setup at home, `diagnose_pi.sh` showed `throttled=0x50005` (under-voltage). Use a proper 5V 2.5A supply with a short cable, and check that it reads `throttled=0x0`. Under-voltage is a known cause of SD-card corruption.
- [ ] **Spare card.** Keep a second tested SD card. A rebuild with Raspberry Pi OS Lite plus `./deploy_to_rpi.sh --setup` takes about 30 minutes.
- [ ] After redeploying on site, note the Pi's new address in your local `.env` (`RPI_HOST`).

## Incident: Nov 2026, the TVs showed the desktop instead of the slideshow

**Cause:** the SD card was failing. Reading the same file twice (with caches cleared) returned different data. That garbled config files and Python code, so the X server crash-looped. The old player depended on the desktop, so it never started, and the TVs showed the desktop.

**How it was found:** repeated `md5sum` of the same file after `echo 3 > /proc/sys/vm/drop_caches` gave different results. This test is now part of `diagnose_pi.sh`.

**Fix:**
- Rebuilt on a tested card with the no-desktop setup (PR #1).
- Removed a log-rotation bug that rewrote about 13 MB of logs every 5 minutes, causing heavy SD wear.
- Added the watchdog, forced HDMI, `diagnose_pi.sh` and `update_pi.sh`.

**Lessons:**
- When the screen misbehaves, run `diagnose_pi.sh` before changing code. Check the *SD card health* and *Power* sections first.
- A cheap USB SD adapter kept disconnecting under sustained load, which made a good card look bad. Test cards in a built-in SD slot or a reliable reader.
- The Pi's own card slot tested fine, so the board isn't suspected. If a known-good card ever shows read errors, suspect the Pi or the power supply.
