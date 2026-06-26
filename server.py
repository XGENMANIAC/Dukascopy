"""
Dukascopy Tick MCP Server
Runs in plain Termux (Android) — pure Python stdlib for all data work.
Only external deps: mcp + starlette/uvicorn for HTTP/SSE transport.
"""

import lzma
import logging
import math
import os
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dukascopy")

# ---------------------------------------------------------------------------
# Instrument point-factor lookup
# Divide raw int32 price fields by this to get real price.
# JPY pairs and XAUUSD use 1 000; most FX use 100 000.
# ---------------------------------------------------------------------------
POINT_FACTORS: dict[str, int] = {
    "USDJPY": 1_000,
    "EURJPY": 1_000,
    "GBPJPY": 1_000,
    "AUDJPY": 1_000,
    "CADJPY": 1_000,
    "CHFJPY": 1_000,
    "NZDJPY": 1_000,
    "XAUUSD": 1_000,
    "XAGUSD": 1_000,
}
DEFAULT_POINT_FACTOR = 100_000

SUPPORTED_INSTRUMENTS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD",
    "NZDUSD", "EURGBP", "EURJPY", "GBPJPY", "XAUUSD", "XAGUSD",
    "EURAUD", "EURCAD", "EURCHF", "GBPAUD", "GBPCAD", "GBPCHF",
    "AUDCAD", "AUDCHF", "AUDNZD", "CADJPY", "CHFJPY", "NZDJPY",
]

# ---------------------------------------------------------------------------
# Dukascopy URL / fetch / decode helpers
# ---------------------------------------------------------------------------

def _url_for(instrument: str, year: int, month_0: int, day: int, hour: int) -> str:
    """
    Build the Dukascopy bi5 URL.
    month_0 is ZERO-indexed (Jan=0, Dec=11) — this is what Dukascopy expects.
    """
    return (
        f"https://datafeed.dukascopy.com/datafeed/"
        f"{instrument.upper()}/"
        f"{year:04d}/{month_0:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
    )


def _fetch_hour_raw(instrument: str, year: int, month_0: int, day: int, hour: int) -> bytes:
    """
    Download one hour's bi5 file. Returns raw bytes (possibly empty).
    Handles 404 and empty body gracefully.
    """
    url = _url_for(instrument, year, month_0, day, hour)
    log.debug("GET %s", url)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.debug("404 (no data) for %s", url)
            return b""
        raise
    except urllib.error.URLError as exc:
        log.warning("Network error fetching %s: %s", url, exc)
        return b""


def _decode_ticks(
    raw: bytes,
    hour_start_utc: datetime,
    point_factor: int,
) -> list[dict[str, Any]]:
    """
    Decompress LZMA bytes, parse 20-byte records, return list of tick dicts.

    Record layout (big-endian): >3i2f
      int32  ms_offset   — ms from hour start
      int32  ask_raw     — divide by point_factor for real ask
      int32  bid_raw     — divide by point_factor for real bid
      float32 ask_vol
      float32 bid_vol

    Malformed trailing bytes (len % 20 != 0) are silently truncated.
    """
    if not raw:
        return []

    try:
        data = lzma.decompress(raw)
    except lzma.LZMAError as exc:
        log.warning("LZMA decompress failed: %s", exc)
        return []

    record_size = 20
    n_complete = len(data) // record_size
    if len(data) % record_size != 0:
        log.warning(
            "Tick data length %d is not divisible by 20; truncating to %d records",
            len(data),
            n_complete,
        )

    ticks: list[dict[str, Any]] = []
    fmt = ">3i2f"
    for i in range(n_complete):
        chunk = data[i * record_size : (i + 1) * record_size]
        ms_off, ask_raw, bid_raw, ask_vol, bid_vol = struct.unpack(fmt, chunk)
        ts = hour_start_utc + timedelta(milliseconds=ms_off)
        ask = ask_raw / point_factor
        bid = bid_raw / point_factor
        ticks.append(
            {
                "ts_utc_iso": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ask": round(ask, 6),
                "bid": round(bid, 6),
                "mid": round((ask + bid) / 2, 6),
                "ask_vol": round(float(ask_vol), 4),
                "bid_vol": round(float(bid_vol), 4),
            }
        )
    return ticks


def fetch_ticks(
    instrument: str,
    year: int,
    month_0: int,
    day: int,
    hour: int,
    throttle_s: float = 0.2,
) -> list[dict[str, Any]]:
    """Fetch + decode a single hour of ticks, with polite throttle."""
    pf = POINT_FACTORS.get(instrument.upper(), DEFAULT_POINT_FACTOR)
    hour_start = datetime(year, month_0 + 1, day, hour, tzinfo=timezone.utc)
    raw = _fetch_hour_raw(instrument, year, month_0, day, hour)
    ticks = _decode_ticks(raw, hour_start, pf)
    time.sleep(throttle_s)
    return ticks


# ---------------------------------------------------------------------------
# Tick classification (tick rule) and footprint builder
# ---------------------------------------------------------------------------

def classify_ticks(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Append 'side' ('buy'/'sell'/'unch') to each tick using the tick rule:
    if mid > prior mid  -> buy
    if mid < prior mid  -> sell
    if unchanged        -> inherit previous side (defaults 'buy' at start)
    Mutates and returns the same list for convenience.
    """
    prev_mid = None
    prev_side = "buy"
    for t in ticks:
        mid = t["mid"]
        if prev_mid is None or mid > prev_mid:
            side = "buy"
        elif mid < prev_mid:
            side = "sell"
        else:
            side = prev_side  # inherit
        t["side"] = side
        prev_mid = mid
        prev_side = side
    return ticks


def build_footprint(
    ticks: list[dict[str, Any]],
    bin_size: float,
    interval_minutes: int,
) -> list[dict[str, Any]]:
    """
    Build a footprint/delta profile from classified ticks.

    Args:
        ticks:            classified tick list (must have 'side' key)
        bin_size:         price bin width (e.g. 0.25 for XAUUSD)
        interval_minutes: candle width in minutes

    Returns a list of interval dicts:
        {
            "interval_start": ISO str,
            "interval_end":   ISO str,
            "bins": [
                {
                    "price_lo": float,  # lower edge of bin
                    "buy_vol":  float,
                    "sell_vol": float,
                    "total_vol":float,
                    "delta":    float,  # buy - sell
                }
            ],
            "total_delta":  float,
            "cum_delta":    float,
            "poc_price_lo": float,  # price_lo of bin with max total_vol
        }
    """
    if not ticks:
        return []

    def _bin_floor(price: float) -> float:
        # floor to nearest bin_size multiple (avoid float jitter)
        return math.floor(price / bin_size + 1e-9) * bin_size

    def _parse_ts(iso: str) -> datetime:
        # e.g. "2024-01-15T10:30:45.123Z"
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )

    interval_td = timedelta(minutes=interval_minutes)

    # Determine first interval start from first tick's timestamp
    first_ts = _parse_ts(ticks[0]["ts_utc_iso"])
    # Snap to interval boundary
    epoch = datetime(first_ts.year, first_ts.month, first_ts.day, tzinfo=timezone.utc)
    elapsed = first_ts - epoch
    intervals_elapsed = int(elapsed.total_seconds() // (interval_minutes * 60))
    interval_start = epoch + timedelta(minutes=intervals_elapsed * interval_minutes)
    interval_end = interval_start + interval_td

    intervals: list[dict[str, Any]] = []
    cum_delta = 0.0

    # bins: dict[float, {"buy_vol": float, "sell_vol": float}]
    current_bins: dict[float, dict[str, float]] = {}

    def _flush(start: datetime, end: datetime, bins: dict) -> None:
        nonlocal cum_delta
        if not bins:
            return
        bin_rows = []
        interval_delta = 0.0
        poc_lo = None
        poc_vol = -1.0
        for price_lo, vols in sorted(bins.items()):
            bv = round(vols["buy_vol"], 4)
            sv = round(vols["sell_vol"], 4)
            tv = round(bv + sv, 4)
            d = round(bv - sv, 4)
            interval_delta += d
            if tv > poc_vol:
                poc_vol = tv
                poc_lo = price_lo
            bin_rows.append(
                {
                    "price_lo": round(price_lo, 6),
                    "buy_vol": bv,
                    "sell_vol": sv,
                    "total_vol": tv,
                    "delta": d,
                }
            )
        cum_delta += interval_delta
        intervals.append(
            {
                "interval_start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "interval_end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "bins": bin_rows,
                "total_delta": round(interval_delta, 4),
                "cum_delta": round(cum_delta, 4),
                "poc_price_lo": round(poc_lo, 6) if poc_lo is not None else None,
            }
        )

    for tick in ticks:
        ts = _parse_ts(tick["ts_utc_iso"])
        # Advance interval if needed
        while ts >= interval_end:
            _flush(interval_start, interval_end, current_bins)
            current_bins = {}
            interval_start = interval_end
            interval_end = interval_start + interval_td

        price_lo = _bin_floor(tick["mid"])
        if price_lo not in current_bins:
            current_bins[price_lo] = {"buy_vol": 0.0, "sell_vol": 0.0}
        vol = tick["ask_vol"] if tick["side"] == "buy" else tick["bid_vol"]
        current_bins[price_lo][f"{tick['side']}_vol"] += vol

    # Flush remaining
    _flush(interval_start, interval_end, current_bins)
    return intervals


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(name="dukascopy-ticks")


@mcp.tool()
def health() -> dict[str, Any]:
    """
    Health check — returns ok status and list of supported instruments.
    Use this to verify the tunnel is up and the server is reachable.
    """
    return {
        "status": "ok",
        "instruments_supported": SUPPORTED_INSTRUMENTS,
        "note": "month in URLs is zero-indexed internally; pass YYYY-MM-DD dates to tools",
    }


@mcp.tool()
def get_ticks(
    instrument: str,
    date: str,
    hour: int | None = None,
    max_ticks: int = 50_000,
) -> list[dict[str, Any]]:
    """
    Fetch historical tick data from Dukascopy.

    Args:
        instrument:  e.g. "XAUUSD", "EURUSD", "USDJPY"
        date:        "YYYY-MM-DD" (UTC)
        hour:        0-23. If omitted, all 24 hours are fetched and concatenated.
        max_ticks:   cap on returned ticks (default 50 000). Raise if you need more.

    Returns a list of tick objects:
        {ts_utc_iso, ask, bid, mid, ask_vol, bid_vol}
    """
    instrument = instrument.upper()

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return [{"error": f"Invalid date '{date}'; use YYYY-MM-DD"}]

    year = dt.year
    month_0 = dt.month - 1  # Dukascopy zero-indexed month
    day = dt.day

    hours_to_fetch = [hour] if hour is not None else list(range(24))
    all_ticks: list[dict[str, Any]] = []

    for h in hours_to_fetch:
        if len(all_ticks) >= max_ticks:
            log.info("max_ticks=%d reached, stopping early", max_ticks)
            break
        try:
            ticks = fetch_ticks(instrument, year, month_0, day, h)
        except Exception as exc:  # noqa: BLE001
            log.warning("Error fetching hour %02d: %s", h, exc)
            ticks = []
        all_ticks.extend(ticks)

    # Trim to max_ticks
    if len(all_ticks) > max_ticks:
        log.info(
            "Trimming from %d to %d ticks (max_ticks limit)",
            len(all_ticks),
            max_ticks,
        )
        all_ticks = all_ticks[:max_ticks]

    return all_ticks


@mcp.tool()
def get_footprint(
    instrument: str,
    date: str,
    hour: int,
    bin_size: float = 0.5,
    interval_minutes: int = 5,
) -> list[dict[str, Any]]:
    """
    Build a footprint / delta profile from one hour of tick data.

    Args:
        instrument:       e.g. "XAUUSD"
        date:             "YYYY-MM-DD" (UTC)
        hour:             0-23 (UTC hour)
        bin_size:         price bin width. Suggested: XAUUSD=0.5, EURUSD=0.0001
        interval_minutes: candle duration in minutes (e.g. 1, 5, 15)

    Returns per-interval footprint rows with per-bin buy/sell/delta volumes
    and interval summary (total_delta, cumulative_delta, POC).
    """
    instrument = instrument.upper()

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return [{"error": f"Invalid date '{date}'; use YYYY-MM-DD"}]

    year = dt.year
    month_0 = dt.month - 1
    day = dt.day

    try:
        ticks = fetch_ticks(instrument, year, month_0, day, hour)
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"Fetch failed: {exc}"}]

    if not ticks:
        return []

    classify_ticks(ticks)
    return build_footprint(ticks, bin_size, interval_minutes)


# ---------------------------------------------------------------------------
# Entry point — HTTP/SSE transport for Cloudflare tunnel
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    # Mutate the settings on the module-level FastMCP instance (tools already
    # registered via @mcp.tool() decorators above, so we can't create a new one).
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = "/mcp"

    # Disable DNS-rebinding protection so Cloudflare tunnel Host headers pass
    # through. The tunnel provides its own TLS layer, so this is safe.
    from mcp.server.fastmcp.server import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    log.info("Starting Dukascopy MCP server on %s:%d (path=/mcp)", host, port)
    mcp.run(transport="streamable-http")
