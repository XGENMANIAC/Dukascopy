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

# Kill any leftover server process from a previous run
if pkill -f "python.*server\.py" 2>/dev/null; then
    echo "[+] Killed leftover server process from previous run"
    sleep 1
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

# pagekite: Python-based tunnel — works on Android because it uses Python's
# socket layer (bionic libc getaddrinfo) rather than a static Go binary.
# Static Go binaries (cloudflared, ngrok) fail on Android because /etc/resolv.conf
# points to [::1]:53 which doesn't exist; this is a read-only system partition.
#
# Setup (one-time):
#   pip install pagekite
#   Sign up at https://pagekite.net/signup/ to get a free permanent kite name.
#   export PAGEKITE_NAME=yourname   (add to ~/.bashrc)
#
# After first authenticated run, config is saved to ~/.pagekite.rc and
# subsequent runs require no interaction.
if [[ -z "${PAGEKITE_NAME:-}" ]]; then
    echo "[!] PAGEKITE_NAME is not set."
    echo "    1. Sign up at https://pagekite.net/signup/ for a free kite name."
    echo "    2. Run: export PAGEKITE_NAME=yourname"
    echo "    3. Re-run this script."
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
fi

echo "[+] Starting pagekite tunnel..."
echo "[+] Stable URL: https://${PAGEKITE_NAME}.pagekite.me"
echo "[+] Add https://${PAGEKITE_NAME}.pagekite.me/mcp to Claude connectors."
echo ""

python -m pagekite "$PORT" "${PAGEKITE_NAME}.pagekite.me" 2>&1

# If the tunnel exits, kill the server too
kill "$SERVER_PID" 2>/dev/null || true
