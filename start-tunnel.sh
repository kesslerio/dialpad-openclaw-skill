#!/run/current-system/sw/bin/bash
# Dialpad Webhook Tunnel Manager
# Starts Cloudflare tunnel and auto-updates Dialpad webhook URL
# 
# REQUIREMENT: cloudflared must be installed and authenticated
# Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation

set -e

SCRIPT_DIR="/home/art/projects/skills/work/dialpad"
PIDFILE="/tmp/dialpad-tunnel.pid"
LOGFILE="/tmp/dialpad-tunnel.log"
CLOUDFLARED="/home/art/.nix-profile/bin/cloudflared"
PYTHON="/home/linuxbrew/.linuxbrew/bin/python3"

# Export env vars for Python scripts
export DIALPAD_API_KEY="${DIALPAD_API_KEY:-}"
export DIALPAD_TELEGRAM_BOT_TOKEN="${DIALPAD_TELEGRAM_BOT_TOKEN:-}"
export DIALPAD_TELEGRAM_CHAT_ID="${DIALPAD_TELEGRAM_CHAT_ID:-}"

# Kill existing tunnel
if [ -f "$PIDFILE" ]; then
    old_pid=$(/run/current-system/sw/bin/cat "$PIDFILE" 2>/dev/null) || true
    if /run/current-system/sw/bin/kill -0 "$old_pid" 2>/dev/null; then
        echo "Stopping existing tunnel (PID: $old_pid)..."
        /run/current-system/sw/bin/kill "$old_pid" 2>/dev/null || true
        /run/current-system/sw/bin/sleep 2
    fi
fi

echo "Starting Cloudflare tunnel to localhost:8888..."
echo "Log: $LOGFILE"

# Start tunnel and capture output
$CLOUDFLARED tunnel --url http://localhost:8888 > "$LOGFILE" 2>&1 &
TUNNEL_PID=$!
/run/current-system/sw/bin/echo $TUNNEL_PID > "$PIDFILE"
echo "Tunnel PID: $TUNNEL_PID"

# Wait for tunnel to establish
/run/current-system/sw/bin/sleep 6

# Extract the tunnel URL
TUNNEL_URL=$(/run/current-system/sw/bin/grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGFILE" | /run/current-system/sw/bin/head -1)

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Failed to get tunnel URL. Check log: $LOGFILE"
    /run/current-system/sw/bin/kill "$TUNNEL_PID" 2>/dev/null || true
    exit 1
fi

echo "✅ Tunnel active: $TUNNEL_URL"

# Update Dialpad webhook
cd "$SCRIPT_DIR" || exit 1
WEBHOOK_URL="${TUNNEL_URL}/webhook/dialpad"

echo "Updating Dialpad webhook to: $WEBHOOK_URL"

# Get existing webhook ID and delete it (best effort)
OLD_WEBHOOK=$($PYTHON create_sms_webhook.py list 2>/dev/null | /run/current-system/sw/bin/grep -oE 'ID: [0-9]+' | /run/current-system/sw/bin/head -1 | /run/current-system/sw/bin/cut -d' ' -f2) || true

if [ -n "$OLD_WEBHOOK" ]; then
    echo "Deleting old webhook: $OLD_WEBHOOK"
    $PYTHON create_sms_webhook.py delete "$OLD_WEBHOOK" 2>/dev/null || true
fi

# Create new webhook
echo "Creating new webhook..."
$PYTHON create_sms_webhook.py create --url "$WEBHOOK_URL" --direction all || echo "⚠️ Failed to create webhook, but tunnel is running"

echo ""
echo "✅ Setup complete!"
echo "Tunnel: $TUNNEL_URL"
echo "Webhook: $WEBHOOK_URL"
echo "PID: $TUNNEL_PID"
echo ""

# Keep the script running so systemd knows the service is active
wait $TUNNEL_PID