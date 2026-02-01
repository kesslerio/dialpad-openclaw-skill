#!/run/current-system/sw/bin/bash
# Simple tunnel starter - run manually or via cron
# This creates a new tunnel and prints the URL
#
# REQUIREMENT: cloudflared must be installed and authenticated
# Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation

LOGFILE="/tmp/dialpad-tunnel-$(date +%s).log"

echo "Starting Cloudflare tunnel..."
echo "Logs: $LOGFILE"

/home/art/.nix-profile/bin/cloudflared tunnel --url http://localhost:8888 > "$LOGFILE" 2>&1 &
PID=$!

echo "Tunnel PID: $PID"
echo ""
echo "Waiting for tunnel to be ready..."
sleep 8

URL=$(/run/current-system/sw/bin/grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGFILE" | /run/current-system/sw/bin/head -1)

if [ -n "$URL" ]; then
    echo "✅ Tunnel URL: $URL"
    echo "Webhook URL: $URL/webhook/dialpad"
    echo ""
    echo "To update Dialpad webhook, run:"
    echo "  cd /home/art/projects/skills/work/dialpad && python3 create_sms_webhook.py create --url \"$URL/webhook/dialpad\" --direction all"
    echo ""
    echo "Test with:"
    echo "  curl $URL/health"
else
    echo "❌ Failed to get tunnel URL. Check logs: $LOGFILE"
    kill $PID 2>/dev/null
fi

echo ""
echo "Tunnel is running in background (PID: $PID)"
echo "To stop: kill $PID"