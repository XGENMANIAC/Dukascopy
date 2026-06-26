#!/data/data/com.termux/files/usr/bin/bash
# Dukascopy MCP Server — Termux start script
# Starts the MCP server, then opens a tunnel via localhost.run (SSH-based,
# no static binary DNS issues on Android) or cloudflared if preferred.
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

echo "[+] Starting SSH tunnel via localhost.run ..."
echo "[+] The public URL will appear below (look for https://...localhost.run)"
echo "[+] Copy that URL + /mcp and add it as a custom MCP connector in Claude."
echo ""

# localhost.run: SSH-based tunnel, uses Termux's dynamic SSH (proper Android DNS).
# No extra binary needed — just openssh (pkg install openssh).
# The URL printed looks like: https://xxxxxxxxxxxxxxxx.localhost.run
# Add /mcp to that URL when configuring Claude's connector.
ssh -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R "80:localhost:$PORT" \
    nokey@localhost.run 2>&1

# If the tunnel exits, kill the server too
kill "$SERVER_PID" 2>/dev/null || true
