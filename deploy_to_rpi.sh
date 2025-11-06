#!/usr/bin/env bash
set -euo pipefail

# Load configuration from .env file
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -E '^RPI_' | xargs)
fi

# Configuration
RPI_USER="${RPI_USER:-srisai}"
RPI_HOST="${RPI_HOST:-192.168.86.23}"
RPI_PASSWORD="${RPI_PASSWORD:-srisai}"
PROJECT_NAME="gdrive2video"
REMOTE_DIR="/home/${RPI_USER}/${PROJECT_NAME}"

echo "=== Deploying gdrive2video to Raspberry Pi ==="
echo "Target: ${RPI_USER}@${RPI_HOST}"
echo ""

# Check if expect is installed
if ! command -v expect &> /dev/null; then
    echo "Error: 'expect' is not installed."
    echo "Please install it with: brew install expect"
    exit 1
fi

# Create temporary expect script for SSH commands
create_expect_ssh() {
    local command="$1"
    cat > /tmp/ssh_expect.exp <<EOF
#!/usr/bin/expect -f
set timeout 30
spawn ssh -o StrictHostKeyChecking=no ${RPI_USER}@${RPI_HOST} "$command"
expect {
    "password:" {
        send "${RPI_PASSWORD}\r"
        expect eof
    }
    eof
}
EOF
    chmod +x /tmp/ssh_expect.exp
    /tmp/ssh_expect.exp
    rm /tmp/ssh_expect.exp
}

# Create temporary expect script for SCP
create_expect_scp() {
    local source="$1"
    local dest="$2"
    cat > /tmp/scp_expect.exp <<EOF
#!/usr/bin/expect -f
set timeout 60
spawn scp -o StrictHostKeyChecking=no -r "$source" "${RPI_USER}@${RPI_HOST}:$dest"
expect {
    "password:" {
        send "${RPI_PASSWORD}\r"
        expect eof
    }
    eof
}
EOF
    chmod +x /tmp/scp_expect.exp
    /tmp/scp_expect.exp
    rm /tmp/scp_expect.exp
}

echo "Step 1: Testing connection..."
create_expect_ssh "echo 'Connection successful' && uname -a"

echo ""
echo "Step 2: Creating remote directory..."
create_expect_ssh "mkdir -p ${REMOTE_DIR}"

echo ""
echo "Step 3: Copying project files..."
# Copy all files except those in .gitignore
create_expect_scp "gdrive_sync.py" "${REMOTE_DIR}/"
create_expect_scp "media_player.py" "${REMOTE_DIR}/"
create_expect_scp "gdrive-sync.service" "${REMOTE_DIR}/"
create_expect_scp "gdrive-sync.timer" "${REMOTE_DIR}/"
create_expect_scp "media-player.service" "${REMOTE_DIR}/"
create_expect_scp "setup_pi.sh" "${REMOTE_DIR}/"
create_expect_scp "README.md" "${REMOTE_DIR}/"

echo ""
echo "Step 4: Copying credentials file..."
if [ -f "gdrive2video-access-key.json" ]; then
    create_expect_scp "gdrive2video-access-key.json" "${REMOTE_DIR}/credentials.json"
    echo "Credentials copied as credentials.json"
elif [ -f "credentials.json" ]; then
    create_expect_scp "credentials.json" "${REMOTE_DIR}/credentials.json"
    echo "Credentials copied"
else
    echo "Warning: No credentials file found. You'll need to copy it manually."
fi

echo ""
echo "Step 5: Setting permissions..."
create_expect_ssh "chmod +x ${REMOTE_DIR}/setup_pi.sh ${REMOTE_DIR}/gdrive_sync.py ${REMOTE_DIR}/media_player.py"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. SSH into the Pi: ssh ${RPI_USER}@${RPI_HOST}"
echo "2. Navigate to: cd ${REMOTE_DIR}"
echo "3. Run setup: ./setup_pi.sh --folder-id YOUR_FOLDER_ID --framebuffer"
echo ""
echo "To SSH directly, run:"
echo "  ssh ${RPI_USER}@${RPI_HOST}"
