#!/usr/bin/env python3
"""
Offline tests for the parsing logic.

The collector's network calls cannot run everywhere (a sandbox proxy will
refuse them; GitHub Actions will not). These tests feed each parser a captured
payload so the logic is verified without a network, and they run in CI on every
push as a guard against a parser silently returning empty.

Run:  python3 scripts/test_parsers.py
"""
import json
import sys
import types
import datetime as dt
from unittest import mock

sys.path.insert(0, "scripts")
import fetch_all as F  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))


def fake_response(payload=None, text=None, content=None):
    r = types.SimpleNamespace()
    r.json = lambda: payload
    r.text = text or ""
    r.content = content or (text or "").encode()
    r.raise_for_status = lambda: None
    return r


# ---------------------------------------------------------------- weather
today = F.now_et().date().isoformat()
OM = {
    "hourly": {
        "time": [f"{today}T{h:02d}:00" for h in range(24)],
        "temperature_2m": [80 + (h % 12) for h in range(24)],
        "apparent_temperature": [86 + (h % 14) for h in range(24)],
        "relative_humidity_2m": [90 - (h % 30) for h in range(24)],
        "precipitation_probability": [10] * 24,
    },
    "daily": {"temperature_2m_max": [94], "temperature_2m_min": [77],
              "apparent_temperature_max": [106], "precipitation_probability_max": [40]},
    "current": {"temperature_2m": 78, "apparent_temperature": 84,
                "relative_humidity_2m": 93, "wind_speed_10m": 11,
                "wind_direction_10m": 90, "time": f"{today}T05:45"},
}
with mock.patch.object(F, "get", lambda *a, **k: fake_response(OM)):
    w = F.fetch_weather()
check("weather ok", w["ok"])
check("weather window is 5 AM to 9 PM", [c["hour"] for c in w["curve"]] == list(range(5, 22)),
      str([c["hour"] for c in w["curve"]])[:60])
check("weather timestamps are today", all(c["time"].startswith(today) for c in w["curve"]))
check("weather high captured", w["today"]["high"] == 94)
check("peak feels matches curve", w["peak_feels"] == max(c["feels"] for c in w["curve"]))

# ---------------------------------------------------------------- alerts
ALERTS = {"features": [{"properties": {
    "event": "Heat Advisory", "severity": "Moderate", "urgency": "Expected",
    "headline": "Heat Advisory until 6 PM EDT", "description": "Heat index up to 108.",
    "onset": "2026-08-25T11:00:00-04:00", "ends": "2026-08-25T18:00:00-04:00"}}]}
with mock.patch.object(F, "get", lambda *a, **k: fake_response(ALERTS)):
    a = F.fetch_alerts()
check("alerts ok", a["ok"])
check("alerts parsed", a["count"] == 1 and a["alerts"][0]["event"] == "Heat Advisory")

with mock.patch.object(F, "get", lambda *a, **k: fake_response({"features": []})):
    a0 = F.fetch_alerts()
check("no alerts is success not failure", a0["ok"] and a0["count"] == 0)

# ---------------------------------------------------------------- air (fallback)
AQ = {"current": {"us_aqi": 118, "pm2_5": 40.2, "pm10": 55.0, "time": f"{today}T05:00"}}
with mock.patch.object(F, "AIRNOW_KEY", ""), \
     mock.patch.object(F, "get", lambda *a, **k: fake_response(AQ)):
    air = F.fetch_air()
check("air fallback ok", air["ok"])
check("AQI 118 maps to Unhealthy for Sensitive Groups",
      air["category"] == "Unhealthy for Sensitive Groups", air.get("category"))

AQ2 = {"current": {"us_aqi": 42, "pm2_5": 8.0, "pm10": 12.0, "time": f"{today}T05:00"}}
with mock.patch.object(F, "AIRNOW_KEY", ""), \
     mock.patch.object(F, "get", lambda *a, **k: fake_response(AQ2)):
    air2 = F.fetch_air()
check("AQI 42 maps to Good", air2["category"] == "Good", air2.get("category"))

# ---------------------------------------------------------------- commute
ROUTE = {"routes": [{"summary": {"lengthInMeters": 32078, "travelTimeInSeconds": 2291,
                                 "trafficDelayInSeconds": 0}}]}
ALTS = {"routes": [
    {"summary": {"lengthInMeters": 39750, "travelTimeInSeconds": 2040}, "sections": [{"t": "toll"}]},
    {"summary": {"lengthInMeters": 36700, "travelTimeInSeconds": 2160}, "sections": [{"t": "toll"}]},
]}
INC = {"incidents": [{"properties": {
    "roadNumbers": ["US-27"], "delay": 240, "magnitudeOfDelay": 2,
    "events": [{"description": "Roadworks"}], "startTime": "2026-08-25T04:00:00Z"}}]}
seq = [ROUTE, ALTS, INC]
with mock.patch.object(F, "TOMTOM_KEY", "test"), \
     mock.patch.object(F, "get", lambda *a, **k: fake_response(seq.pop(0))):
    c = F.fetch_commute()
check("commute ok", c["ok"])
check("32,078 m converts to 19.9 mi", c["miles"] == 19.9, str(c.get("miles")))
check("2,291 s converts to 38 min", c["minutes"] == 38, str(c.get("minutes")))
check("alternates captured", len(c["alternates"]) == 2)
check("only >10 min savings surface",
      c["alternates_worth_taking"] == [], str(c["alternates_worth_taking"]))
check("incident captured with road", c["incident_count"] == 1 and "US-27" in c["incidents"][0]["roads"])

with mock.patch.object(F, "TOMTOM_KEY", ""):
    cn = F.fetch_commute()
check("missing key fails loudly, not silently", cn["ok"] is False and "TOMTOM_API_KEY" in cn["error"])

# ---------------------------------------------------------------- tropics
NHC = """<html><body><pre>
Tropical Weather Outlook
NWS National Hurricane Center Miami FL
200 AM EDT Tue Aug 25 2026

Central Subtropical Atlantic (AL95):
Showers associated with a low pressure area southeast of Bermuda.
* Formation chance through 48 hours...high...70 percent.
* Formation chance through 7 days...high...80 percent.

Eastern Tropical Atlantic:
A tropical wave is forecast to move off the west coast of Africa.
* Formation chance through 48 hours...low...10 percent.
* Formation chance through 7 days...medium...50 percent.
</pre></body></html>"""
with mock.patch.object(F, "get", lambda *a, **k: fake_response(text=NHC)):
    t = F.fetch_tropics()
check("tropics ok", t["ok"])
check("tropics issue time parsed", t["issued"] and "Aug 25 2026" in t["issued"], str(t.get("issued")))
check("two areas found", len(t["areas"]) == 2, str(len(t["areas"])))
check("AL95 reads 70/80", t["areas"][0]["chance_48h"] == 70 and t["areas"][0]["chance_7d"] == 80,
      str(t["areas"][0]))
check("Africa wave reads 10/50", t["areas"][1]["chance_48h"] == 10 and t["areas"][1]["chance_7d"] == 50)

# ---------------------------------------------------------------- news
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Wildfire grows to 15,505 acres</title>
<link>https://example.com/a</link><pubDate>Mon, 25 Aug 2026 04:10:00 -0400</pubDate></item>
<item><title>City council meets</title>
<link>https://example.com/b</link><pubDate>Mon, 25 Aug 2026 03:00:00 -0400</pubDate></item>
</channel></rss>"""
with mock.patch.object(F, "get", lambda *a, **k: fake_response(content=RSS.encode())):
    items = F._parse_feed("https://example.com/feed")
check("rss parsed", len(items) == 2 and items[0]["title"].startswith("Wildfire"))
check("rss keeps pubDate", items[0]["published"] and "25 Aug 2026" in items[0]["published"])

with mock.patch.object(F, "get", lambda *a, **k: fake_response(content=RSS.encode())):
    fires = F.fetch_fires()
check("fire keyword filter works", len(fires["items"]) == 1 and "acres" in fires["items"][0]["title"])

# ---------------------------------------------------------------- failure shape
f = F.fail("SomeSource", "boom")
check("failure block has ok False", f["ok"] is False)
check("failure block names the source", f["source"] == "SomeSource")
check("failure block is serialisable", json.dumps(f) and True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
