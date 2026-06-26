# Dukascopy Tick MCP Server (Termux + Cloudflare)

A Python MCP server that fetches and decodes Dukascopy historical tick data and
exposes it as Claude MCP tools. Runs entirely on-device in **plain Termux** on
Android — no proot, no pandas, no numpy — using only the Python stdlib for all
data work.

---

## What it provides

| Tool | Description |
|------|-------------|
| `health()` | Ping / connectivity check |
| `get_ticks(instrument, date, hour?, max_ticks?)` | Fetch raw tick data (bid/ask/mid/vol) |
| `get_footprint(instrument, date, hour, bin_size, interval_minutes)` | Footprint chart with delta profile |

---

## Setup from scratch on Termux

### 1. Install Termux from F-Droid (not Play Store)

The F-Droid version ships a working package repo. The Play Store version is
outdated and unsupported.

### 2. Update packages and install Python

```bash
pkg update && pkg upgrade -y
pkg install python -y
```

Verify: `python --version` should show 3.11 or later.

### 3. Install cloudflared (ARM64 binary)

Termux runs on ARM64. Download the static cloudflared binary directly:

```bash
# Create a local bin dir if it doesn't exist
mkdir -p $HOME/.local/bin

# Download cloudflared ARM64 (check https://github.com/cloudflare/cloudflared/releases
# for the latest version and update the URL accordingly)
curl -fsSL \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" \
  -o "$HOME/.local/bin/cloudflared"

chmod +x "$HOME/.local/bin/cloudflared"

# Add to PATH (add this line to ~/.bashrc or ~/.zshrc too)
export PATH="$HOME/.local/bin:$PATH"

# Verify
cloudflared --version
```

### 4. Clone this repo and install Python deps

```bash
pkg install git -y
git clone https://github.com/xgenmaniac/dukascopy.git
cd dukascopy

pip install -r requirements.txt
```

> **Why no pandas/numpy?**  
> Termux's Python is a pure ARM64 build with no BLAS/LAPACK. Pip cannot build
> wheels for pandas or numpy from source in plain Termux without proot.  
> This server uses only `urllib`, `lzma`, `struct`, `datetime`, `math`, and
> `json` from the stdlib — no C extensions needed.

### 5. (Recommended) Prevent Android from killing the process

```bash
termux-wake-lock
```

This keeps the CPU running and prevents the kernel from OOM-killing Termux in
the background. You need `Termux:API` app installed from F-Droid for this.
Alternatively, enable **"Battery optimization: unrestricted"** for Termux in
Android Settings.

---

## Running the server

```bash
bash run.sh
```

This will:
1. Acquire a Termux wake lock (if available)
2. Start the MCP server on `http://localhost:8000`
3. Start `cloudflared tunnel` and print a public `trycloudflare.com` URL

Example output:
```
=== Dukascopy MCP Server ===
Port: 8000
[+] Starting MCP server...
[+] MCP server PID: 12345
[+] Starting Cloudflare tunnel...
...
https://example-random-words.trycloudflare.com
```

Copy that `https://` URL.

### Custom port

```bash
PORT=9000 bash run.sh
```

---

## Adding the server to Claude

1. Open Claude → **Settings** → **Connectors**
2. Click **"Add custom connector"** (or **"Add MCP server"**)
3. Paste your `https://xxxxx.trycloudflare.com/mcp` URL
   - The path suffix `/mcp` is required (that's the streamable-HTTP endpoint)
4. Save. You should immediately see the three tools available in your chat.

To test, ask Claude: _"Call the health tool on the Dukascopy connector."_

---

## Important caveats

### Tunnel URL rotates on restart
Every time you run `run.sh`, Cloudflare assigns a **new random URL**.  
You must update the connector URL in Claude each time you restart.

### Server dies when Termux is killed
Android aggressively kills background apps. To keep the server alive:
- Use `termux-wake-lock` (requires Termux:API)
- Disable battery optimization for Termux in Android Settings
- Keep Termux in the foreground or use a notification

### Dukascopy data notes
- All timestamps are **UTC**.
- The Dukascopy month in URLs is **zero-indexed** (Jan = `00`, Dec = `11`).
  The tools accept standard `YYYY-MM-DD` dates — the zero-indexing is handled
  internally.
- Hours with no trading (weekends, holidays) return empty tick lists — not an error.
- XAUUSD and JPY pairs use a price scale factor of 1 000; most FX pairs use 100 000.

---

## Running the self-test

```bash
python selftest.py
```

Expected output (with network):
```
[PASS] URL builder: https://datafeed.dukascopy.com/datafeed/XAUUSD/2024/00/15/10h_ticks.bi5
[PASS] Empty body decodes to []
[PASS] No forbidden imports in server.py
[INFO] Fetching LIVE data: XAUUSD 2024-01-15 hour 10 UTC ...
[INFO] Tick count: 12345
[INFO] First tick: {ts_utc_iso: ..., bid: ..., ask: ..., ...}
[PASS] Live fetch returned valid tick dicts
[PASS] classify_ticks: sides found = {'buy', 'sell'}
[INFO] Footprint intervals: 12
[PASS] build_footprint returned valid structure
[DONE] All tests completed.
```

---

## Tool reference

### `health()`
```json
{"status": "ok", "instruments_supported": ["EURUSD", "XAUUSD", ...]}
```

### `get_ticks(instrument, date, hour=None, max_ticks=50000)`
```python
get_ticks("XAUUSD", "2024-01-15", hour=10)
# Returns:
[
  {
    "ts_utc_iso": "2024-01-15T10:00:00.123Z",
    "ask": 2023.456,
    "bid": 2023.123,
    "mid": 2023.289,
    "ask_vol": 1.5,
    "bid_vol": 0.8
  },
  ...
]
```
Omit `hour` to get all 24 hours (up to `max_ticks`).

### `get_footprint(instrument, date, hour, bin_size=0.5, interval_minutes=5)`
```python
get_footprint("XAUUSD", "2024-01-15", 10, bin_size=0.5, interval_minutes=5)
# Returns per-interval footprint:
[
  {
    "interval_start": "2024-01-15T10:00:00.000Z",
    "interval_end":   "2024-01-15T10:05:00.000Z",
    "bins": [
      {"price_lo": 2022.5, "buy_vol": 12.3, "sell_vol": 8.1, "total_vol": 20.4, "delta": 4.2},
      ...
    ],
    "total_delta": 4.2,
    "cum_delta": 4.2,
    "poc_price_lo": 2023.0
  },
  ...
]
```

Suggested `bin_size` values:
| Instrument | bin_size |
|------------|----------|
| XAUUSD     | 0.5      |
| EURUSD     | 0.0001   |
| USDJPY     | 0.01     |
| GBPUSD     | 0.0001   |
