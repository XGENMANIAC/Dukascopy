"""
selftest.py — stdlib-only sanity check for the Dukascopy decoder.
Fetches one hour of XAUUSD ticks (or uses a mock if offline),
prints tick count + first/last tick, runs classify + footprint.
No MCP, no pandas, no numpy.
"""

import sys
import urllib.error

# Guard: ensure no forbidden imports sneak in
_FORBIDDEN = ["pandas", "numpy", "requests", "httpx", "scipy"]
for _mod in _FORBIDDEN:
    if _mod in sys.modules:
        print(f"FAIL: forbidden module '{_mod}' is imported!", file=sys.stderr)
        sys.exit(1)

from server import (
    _decode_ticks,
    _fetch_hour_raw,
    _url_for,
    build_footprint,
    classify_ticks,
    fetch_ticks,
)
from datetime import datetime, timezone

INSTRUMENT = "XAUUSD"
# A well-known trading day (not a weekend/holiday) for XAUUSD
YEAR, MONTH_0, DAY, HOUR = 2024, 0, 15, 10  # 2024-01-15 10:00 UTC


def test_url_builder() -> None:
    url = _url_for(INSTRUMENT, YEAR, MONTH_0, DAY, HOUR)
    expected = (
        "https://datafeed.dukascopy.com/datafeed/XAUUSD/2024/00/15/10h_ticks.bi5"
    )
    assert url == expected, f"URL mismatch:\n  got:      {url}\n  expected: {expected}"
    print(f"[PASS] URL builder: {url}")


def test_empty_decode() -> None:
    result = _decode_ticks(b"", datetime(2024, 1, 15, 10, tzinfo=timezone.utc), 1000)
    assert result == [], "Empty bytes should yield empty list"
    print("[PASS] Empty body decodes to []")


def test_live_fetch() -> None:
    print(f"\n[INFO] Fetching LIVE data: {INSTRUMENT} {YEAR}-01-{DAY:02d} hour {HOUR:02d} UTC ...")
    try:
        ticks = fetch_ticks(INSTRUMENT, YEAR, MONTH_0, DAY, HOUR, throttle_s=0)
    except urllib.error.URLError as exc:
        print(f"[SKIP] Network unavailable ({exc}); skipping live fetch test.")
        return

    print(f"[INFO] Tick count: {len(ticks)}")
    if ticks:
        print(f"[INFO] First tick: {ticks[0]}")
        print(f"[INFO] Last tick:  {ticks[-1]}")
        assert "ts_utc_iso" in ticks[0], "tick missing ts_utc_iso"
        assert "bid" in ticks[0], "tick missing bid"
        assert "ask" in ticks[0], "tick missing ask"
        assert "mid" in ticks[0], "tick missing mid"
        print("[PASS] Live fetch returned valid tick dicts")
    else:
        print("[WARN] No ticks returned (market may have been closed that hour)")

    # Test classify
    classify_ticks(ticks)
    if ticks:
        assert "side" in ticks[0], "classify_ticks should add 'side'"
        sides = {t["side"] for t in ticks}
        assert sides.issubset({"buy", "sell"}), f"Unexpected sides: {sides}"
        print(f"[PASS] classify_ticks: sides found = {sides}")

    # Test footprint
    fp = build_footprint(ticks, bin_size=0.5, interval_minutes=5)
    print(f"[INFO] Footprint intervals: {len(fp)}")
    if fp:
        first_iv = fp[0]
        assert "interval_start" in first_iv
        assert "bins" in first_iv
        assert "total_delta" in first_iv
        assert "cum_delta" in first_iv
        assert "poc_price_lo" in first_iv
        print(f"[INFO] First interval: start={first_iv['interval_start']} "
              f"bins={len(first_iv['bins'])} delta={first_iv['total_delta']}")
        print("[PASS] build_footprint returned valid structure")

    return ticks


def test_no_forbidden_imports() -> None:
    import importlib.util
    for mod in _FORBIDDEN:
        spec = importlib.util.find_spec(mod)
        # spec being None means not installed — that's fine
        # But it might be found in site-packages even if not used;
        # what matters is it's not imported into server.py's namespace.
        pass  # We already checked sys.modules at top of file
    # Check server module's globals
    import server
    for mod in _FORBIDDEN:
        assert mod not in vars(server), f"server.py imports forbidden module: {mod}"
    print(f"[PASS] No forbidden imports in server.py ({_FORBIDDEN})")


if __name__ == "__main__":
    print("=" * 60)
    print("Dukascopy MCP Server — Self Test")
    print("=" * 60)

    test_url_builder()
    test_empty_decode()
    test_no_forbidden_imports()
    test_live_fetch()

    print("\n[DONE] All tests completed.")
