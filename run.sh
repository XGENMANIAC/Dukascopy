#!/data/data/com.termux/files/usr/bin/bash
# Dukascopy MCP Server — Termux start script
# Starts the MCP server, then opens a tunnel via localhost.run (SSH-based,
# no static binary DNS issues on Android) or cloudflared if preferred.
# The public URL is printed to stdout — add it as a custom connector in Claude.

set -euo pipefail

PORT="${PORT:-8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Source ~/.bashrc so exports like NGROK_DOMAIN are available when run.sh is
# invoked from a non-interactive shell (which doesn't auto-source ~/.bashrc).
# shellcheck disable=SC1090
[[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc" 2>/dev/null || true

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

# ngrok static domain via proot DNS fix.
# Static Go binaries (ngrok, cloudflared) read /etc/resolv.conf which on Android
# points to [::1]:53 — a non-existent stub. /etc/resolv.conf is on a read-only
# system partition. proot bind-mounts a user-writable resolv.conf over it for
# just the ngrok child process — no root required.
#
# Setup (one-time):
#   pkg install proot
#   mkdir -p $HOME/.local/bin
#   curl -fsSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz \
#     | tar xz -C $HOME/.local/bin
#   ngrok config add-authtoken YOUR_TOKEN
#   export NGROK_DOMAIN=yourname.ngrok-free.dev   (add to ~/.bashrc)

if [[ -z "${NGROK_DOMAIN:-}" ]]; then
    echo "[!] NGROK_DOMAIN is not set."
    echo "    export NGROK_DOMAIN=yourname.ngrok-free.dev  (add to ~/.bashrc)"
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
fi

if ! command -v proot &>/dev/null; then
    echo "[!] proot not found. Run: pkg install proot"
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
fi

# Write real nameservers to a user-writable path; proot mounts it over /etc/resolv.conf
printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > "$HOME/.resolv.conf"

echo "[+] Starting ngrok tunnel (proot DNS fix active)..."
echo "[+] Stable URL: https://${NGROK_DOMAIN}"
echo "[+] Add https://${NGROK_DOMAIN}/mcp to Claude connectors."
echo ""

proot -b "$HOME/.resolv.conf:/etc/resolv.conf" \
    ngrok http --url="$NGROK_DOMAIN" "$PORT" 2>&1

# If the tunnel exits, kill the server too
kill "$SERVER_PID" 2>/dev/null || true
