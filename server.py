"""
Dukascopy Tick MCP Server
Runs in plain Termux (Android) — pure Python stdlib for all data work.
External deps: starlette + uvicorn only (pure Python, no Rust/C needed).
MCP JSON-RPC 2.0 protocol implemented directly — no mcp/pydantic-core required.
"""

import json
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

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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
    Append 'side' ('buy'/'sell') to each tick using the tick rule:
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

    Returns a list of interval dicts with per-bin buy/sell/delta volumes
    and interval summary (total_delta, cumulative_delta, POC).
    """
    if not ticks:
        return []

    def _bin_floor(price: float) -> float:
        # floor to nearest bin_size multiple (avoid float jitter)
        return math.floor(price / bin_size + 1e-9) * bin_size

    def _parse_ts(iso: str) -> datetime:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )

    interval_td = timedelta(minutes=interval_minutes)

    first_ts = _parse_ts(ticks[0]["ts_utc_iso"])
    epoch = datetime(first_ts.year, first_ts.month, first_ts.day, tzinfo=timezone.utc)
    elapsed = first_ts - epoch
    intervals_elapsed = int(elapsed.total_seconds() // (interval_minutes * 60))
    interval_start = epoch + timedelta(minutes=intervals_elapsed * interval_minutes)
    interval_end = interval_start + interval_td

    intervals: list[dict[str, Any]] = []
    cum_delta = 0.0
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

    _flush(interval_start, interval_end, current_bins)
    return intervals


# ---------------------------------------------------------------------------
# Tool functions (plain Python — no MCP decorator needed)
# ---------------------------------------------------------------------------

def health() -> dict[str, Any]:
    """Health check."""
    return {
        "status": "ok",
        "instruments_supported": SUPPORTED_INSTRUMENTS,
        "note": "month in URLs is zero-indexed internally; pass YYYY-MM-DD dates to tools",
    }


def get_ticks(
    instrument: str,
    date: str,
    hour: int | None = None,
    max_ticks: int = 50_000,
) -> list[dict[str, Any]]:
    """Fetch historical tick data from Dukascopy."""
    instrument = instrument.upper()
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return [{"error": f"Invalid date '{date}'; use YYYY-MM-DD"}]

    year = dt.year
    month_0 = dt.month - 1
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

    if len(all_ticks) > max_ticks:
        all_ticks = all_ticks[:max_ticks]

    return all_ticks


def get_footprint(
    instrument: str,
    date: str,
    hour: int,
    bin_size: float = 0.5,
    interval_minutes: int = 5,
) -> list[dict[str, Any]]:
    """Build a footprint / delta profile from one hour of tick data."""
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
# MCP JSON-RPC protocol — hand-rolled, no pydantic/mcp library needed
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2024-11-05"

TOOL_SCHEMAS = [
    {
        "name": "health",
        "description": (
            "Health check — returns ok status and list of supported instruments. "
            "Use this to verify the tunnel is up and the server is reachable."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_ticks",
        "description": (
            "Fetch historical tick data from Dukascopy. "
            "Returns list of {ts_utc_iso, ask, bid, mid, ask_vol, bid_vol}. "
            "Omit hour to get all 24h (up to max_ticks)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instrument": {
                    "type": "string",
                    "description": "e.g. XAUUSD, EURUSD, USDJPY",
                },
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD (UTC)",
                },
                "hour": {
                    "type": "integer",
                    "description": "0-23. If omitted, all 24 hours are fetched and concatenated.",
                },
                "max_ticks": {
                    "type": "integer",
                    "description": "Cap on returned ticks (default 50000).",
                },
            },
            "required": ["instrument", "date"],
        },
    },
    {
        "name": "get_footprint",
        "description": (
            "Build a footprint / delta profile from one hour of tick data. "
            "Returns per-interval rows with per-bin buy/sell/delta volumes, "
            "total_delta, cumulative_delta, and POC (point of control)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "description": "e.g. XAUUSD"},
                "date": {"type": "string", "description": "YYYY-MM-DD (UTC)"},
                "hour": {"type": "integer", "description": "0-23 (UTC hour)"},
                "bin_size": {
                    "type": "number",
                    "description": "Price bin width. Suggested: XAUUSD=0.5, EURUSD=0.0001",
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Candle width in minutes (e.g. 1, 5, 15)",
                },
            },
            "required": ["instrument", "date", "hour"],
        },
    },
]

TOOL_MAP = {
    "health": health,
    "get_ticks": get_ticks,
    "get_footprint": get_footprint,
}


def _jsonrpc_ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_err(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


async def handle_mcp(request: Request) -> Response:
    """Single POST /mcp endpoint — handles all MCP JSON-RPC methods."""
    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_err(None, -32700, "Parse error")

    req_id = body.get("id")  # None for notifications
    method = body.get("method", "")
    params = body.get("params") or {}

    log.info("MCP %s id=%s", method, req_id)

    try:
        if method == "initialize":
            return _jsonrpc_ok(
                req_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "dukascopy-ticks", "version": "1.0.0"},
                },
            )

        if method == "notifications/initialized":
            # Notification — no id, no response body required
            return Response(status_code=204)

        if method == "ping":
            return _jsonrpc_ok(req_id, {})

        if method == "tools/list":
            return _jsonrpc_ok(req_id, {"tools": TOOL_SCHEMAS})

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments") or {}
            fn = TOOL_MAP.get(tool_name)
            if fn is None:
                return _jsonrpc_err(req_id, -32602, f"Unknown tool: {tool_name!r}")
            result = fn(**arguments)
            return _jsonrpc_ok(
                req_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ]
                },
            )

        return _jsonrpc_err(req_id, -32601, f"Method not found: {method!r}")

    except TypeError as exc:
        # Bad arguments passed to tool function
        return _jsonrpc_err(req_id, -32602, f"Invalid params: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Internal error handling %s", method)
        return _jsonrpc_err(req_id, -32603, f"Internal error: {exc}")


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------

app = Starlette(
    routes=[
        Route("/mcp", handle_mcp, methods=["POST"]),
        # OPTIONS for CORS preflight (Cloudflare tunnel / browser clients)
        Route("/mcp", lambda r: Response(status_code=204), methods=["OPTIONS"]),
    ]
)


# ---------------------------------------------------------------------------
# Entry point — bind to 0.0.0.0 so cloudflared can reach it
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    log.info("Starting Dukascopy MCP server on %s:%d (POST /mcp)", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
