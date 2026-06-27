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

### 3. Set up ngrok with a static free domain and proot DNS fix

Static Go binaries (cloudflared, ngrok) fail DNS on stock Android because
`/etc/resolv.conf` points to `[::1]:53` — a non-existent IPv6 stub on a read-only
system partition. `proot` (available in Termux, no root needed) can bind-mount a
writable `resolv.conf` over it for just the ngrok process, fixing the issue.

```bash
# Install proot — fixes Android DNS for ngrok without root
pkg install proot

# Download ngrok ARM64 binary
mkdir -p $HOME/.local/bin
curl -fsSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz \
  | tar xz -C $HOME/.local/bin
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc too

# Sign up at https://ngrok.com and get a free static domain, then:
ngrok config add-authtoken YOUR_TOKEN_HERE

# Set your static domain (add to ~/.bashrc so it persists)
export NGROK_DOMAIN=yourname.ngrok-free.dev
```

Your permanent URL will be `https://yourname.ngrok-free.dev` — it never changes
across restarts.

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
[+] Starting ngrok tunnel (proot DNS fix active)...
[+] Stable URL: https://yourname.ngrok-free.dev
[+] Add https://yourname.ngrok-free.dev/mcp to Claude connectors.
```

The URL is permanent — it never changes on restart.

### Custom port

```bash
PORT=9000 bash run.sh
```

---

## Adding the server to Claude

1. Open Claude → **Settings** → **Connectors**
2. Click **"Add custom connector"** (or **"Add MCP server"**)
3. Paste `https://yourname.ngrok-free.dev/mcp` (replace with your static ngrok domain)
   - The path suffix `/mcp` is required (that's the streamable-HTTP endpoint)
4. Save. You should immediately see the three tools available in your chat.

To test, ask Claude: _"Call the health tool on the Dukascopy connector."_

---

## Important caveats

### Tunnel URL is stable across restarts
ngrok free static domains (`yourname.ngrok-free.dev`) are permanent.
You configure the connector URL in Claude once and never need to update it.

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
