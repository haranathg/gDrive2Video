#!/usr/bin/env bash
# Update this Pi to the latest code on GitHub. Runs only when you start it.
#
# On the Pi:            ~/gdrive2video/update_pi.sh
# From a Mac on the same network:
#                       ssh srisai@<pi-address> ~/gdrive2video/update_pi.sh
#
# Options:
#   update_pi.sh [BRANCH]    update from BRANCH instead of main
#   update_pi.sh --force     re-apply even if the code hasn't changed
#
# Only code tracked in git is replaced. credentials.json, media/, logs/,
# .venv/ and /etc/gdrive2video.env are left alone.
set -euo pipefail

REPO_URL="https://github.com/haranathg/gDrive2Video.git"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="main"
FORCE=false
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=true ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) BRANCH="${arg}" ;;
  esac
done

cd "${PROJECT_DIR}"
command -v git >/dev/null || { echo "Installing git..."; sudo apt-get install -y git; }

# The first install may have been copied over rather than cloned; turn it into
# a git checkout so it can be updated in place.
if [[ ! -d .git ]]; then
  git init -q
  git remote add origin "${REPO_URL}"
fi

echo "Fetching ${BRANCH} from GitHub..."
git fetch -q origin "${BRANCH}"
OLD="$(git rev-parse -q --verify HEAD || echo none)"
NEW="$(git rev-parse FETCH_HEAD)"

if [[ "${OLD}" == "${NEW}" ]] && ! ${FORCE}; then
  echo "Already up to date ($(git log -1 --format='%h %s'))."
  exit 0
fi

# Check the new code before switching to it, so a broken push can't take the
# screens down.
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
git archive "${NEW}" | tar -x -C "${TMP}"
if ! python3 -m py_compile "${TMP}/media_player.py" "${TMP}/gdrive_sync.py" ||
   ! bash -n "${TMP}/setup_pi.sh"; then
  echo "The new code failed basic checks; NOT updating. The Pi stays on the current version." >&2
  exit 1
fi

# -f: replace tracked code files even if a copied-in version is in the way.
git checkout -q -f -B "${BRANCH}" "${NEW}"
chmod +x ./*.sh ./*.py

echo
if [[ "${OLD}" != "none" ]]; then
  echo "Changes:"
  git log --oneline "${OLD}..${NEW}" 2>/dev/null || true
fi
echo "Now on: $(git log -1 --format='%h %s')"

echo
echo "Applying (services, Python packages, settings; OS packages are left to automatic updates)..."
./setup_pi.sh --no-upgrade
sudo systemctl restart media-player.service

echo
echo "Update complete. Check with: ${PROJECT_DIR}/diagnose_pi.sh"
