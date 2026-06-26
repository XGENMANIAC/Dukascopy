#!/data/data/com.termux/files/usr/bin/bash
# Dukascopy MCP Server — Termux start script
# Starts the MCP server, then opens a Cloudflare trycloudflare.com tunnel.
# The public URL is printed to stdout — add it as a custom connector in Claude.

set -euo pipefail

PORT="${PORT:-8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Dukascopy MCP Server ==="
echo "Port: $PORT"
echo ""

# Optional: keep Termux awake so Android doesn't kill the process
if command -v termux-wake-lock &>/dev/null; then
    echo "[+] Acquiring Termux wake lock..."
    termux-wake-lock
fi

# Start the MCP server in the background
echo "[+] Starting MCP server..."
PORT="$PORT" python "$SCRIPT_DIR/server.py" &
SERVER_PID=$!
echo "[+] MCP server PID: $SERVER_PID"

# Wait briefly for the server to bind
sleep 2

# Check it started
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[!] Server failed to start. Check logs above."
    exit 1
fi

echo "[+] Starting Cloudflare tunnel on http://localhost:$PORT ..."
echo "[+] The public trycloudflare.com URL will appear below."
echo "[+] Copy the https://....trycloudflare.com URL and add it as a custom"
echo "    MCP connector in Claude (Settings -> Connectors -> Add custom connector)."
echo ""

# cloudflared prints the URL to stderr; tee it so the user sees it
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | \
    grep --line-buffered -E '(trycloudflare\.com|ERR|error|failed)' || true

# If cloudflared exits, kill the server too
kill "$SERVER_PID" 2>/dev/null || true
