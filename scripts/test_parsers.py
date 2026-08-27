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
INC_EMPTY = {"incidents": []}
OUTBOUND = F.COMMUTE_LEGS[0]
RETURN = F.COMMUTE_LEGS[1]

# Incidents are collected first (one call per county), then the route, then
# the alternates. Pin the leg so the test does not depend on the wall clock.
seq = [INC, INC_EMPTY, ROUTE, ALTS]
with mock.patch.object(F, "TOMTOM_KEY", "test"), \
     mock.patch.object(F, "_active_leg", lambda *a, **k: OUTBOUND), \
     mock.patch.object(F, "get", lambda *a, **k: fake_response(seq.pop(0))):
    c = F.fetch_commute()
check("commute ok", c["ok"])
check("outbound leg named", c["leg"].endswith("Jackson Memorial") and c["direction"] == "outbound")
check("route is shown in window", c["route_shown"] is True)
check("32,078 m converts to 19.9 mi", c["miles"] == 19.9, str(c.get("miles")))
check("2,291 s converts to 38 min", c["minutes"] == 38, str(c.get("minutes")))
check("alternates captured", len(c["alternates"]) == 2)
check("only >10 min savings surface",
      c["alternates_worth_taking"] == [], str(c["alternates_worth_taking"]))
check("incident captured with road", c["incident_count"] == 1 and "US-27" in c["incidents"][0]["roads"])

with mock.patch.object(F, "TOMTOM_KEY", ""):
    cn = F.fetch_commute()
check("missing key fails loudly, not silently", cn["ok"] is False and "TOMTOM_API_KEY" in cn["error"])

# ---------------------------------------------------------------- commute windows
def at(h, m=0, day=27):        # Thu 27 Aug 2026 is a weekday
    return dt.datetime(2026, 8, day, h, m, tzinfo=F.ET)

check("5 AM routes outbound", F._active_leg(at(5))["key"] == "outbound")
check("8:59 AM still outbound", F._active_leg(at(8, 59))["key"] == "outbound")
check("2 PM routes return", F._active_leg(at(14))["key"] == "return")
check("5 PM still return", F._active_leg(at(17))["key"] == "return")
check("10 AM has no leg", F._active_leg(at(10)) is None)
check("9 PM has no leg", F._active_leg(at(21)) is None)
check("weekend has no leg", F._active_leg(at(9, 0, 29)) is None)   # Sat 29 Aug

check("outbound departs 5:45 when ahead",
      F._depart_at(OUTBOUND, at(4)).endswith("05:45:00-04:00"))
check("past the clock inside window asks for live",
      F._depart_at(OUTBOUND, at(7)) == "now")
check("outside window rolls to tomorrow",
      F._depart_at(OUTBOUND, at(23)).startswith("2026-08-28"))
check("return departs 15:30", F._depart_at(RETURN, at(13)).endswith("15:30:00-04:00"))

# Outside any window the card still carries area traffic, just no route.
seq2 = [INC, INC_EMPTY]
with mock.patch.object(F, "TOMTOM_KEY", "test"), \
     mock.patch.object(F, "_active_leg", lambda *a, **k: None), \
     mock.patch.object(F, "get", lambda *a, **k: fake_response(seq2.pop(0))):
    cq = F.fetch_commute()
check("no leg still returns traffic", cq["ok"] and cq["route_shown"] is False)
check("no leg means no travel time", "minutes" not in cq)
check("no leg still counts incidents", cq["incident_count"] == 1)

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

# ---------------------------------------------------------------- county traffic
INC = {
    "Broward": {"incidents": [
        {"properties": {"roadNumbers": ["I-95"], "delay": 300, "magnitudeOfDelay": 2,
                        "events": [{"description": "Slow traffic"}], "startTime": "2026-08-27T11:00:00Z"}},
        {"properties": {"roadNumbers": ["I-595"], "delay": 60, "magnitudeOfDelay": 4,
                        "events": [{"description": "Road closed"}], "startTime": "2026-08-27T10:00:00Z"}},
    ]},
    "Miami-Dade": {"incidents": [
        {"properties": {"roadNumbers": ["SR-826"], "delay": 900, "magnitudeOfDelay": 3,
                        "events": [{"description": "Accident"}], "startTime": "2026-08-27T11:30:00Z"}},
        # same incident the Broward box also sees, in the overlap band
        {"properties": {"roadNumbers": ["I-95"], "delay": 300, "magnitudeOfDelay": 2,
                        "events": [{"description": "Slow traffic"}], "startTime": "2026-08-27T11:00:00Z"}},
    ]},
}


def _inc_by_bbox(url, params=None, **k):
    lat_lo = float(params["bbox"].split(",")[1])
    return fake_response(INC["Broward" if lat_lo > 25.9 else "Miami-Dade"])


F.TOMTOM_KEY = "test-key"
with mock.patch.object(F, "get", _inc_by_bbox):
    inc = F._area_incidents()

check("both counties collected", {i["county"] for i in inc} == {"Broward", "Miami-Dade"})
check("overlap deduplicated", len(inc) == 3, f"got {len(inc)}")
check("closure sorts first", inc[0]["magnitude"] == 4 and "closed" in inc[0]["description"].lower())
check("then worst delay first", inc[1]["delay_s"] == 900 and inc[1]["roads"] == ["SR-826"])
check("county is labelled", all(i["county"] in ("Broward", "Miami-Dade") for i in inc))

# ---------------------------------------------------------------- feed media
MEDIA_RSS = b"""<?xml version="1.0"?>
<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Clip with video</title><link>https://x.test/a</link>
  <pubDate>Wed, 27 Aug 2026 09:00:00 -0400</pubDate>
  <enclosure url="https://x.test/a.mp4" type="video/mp4"/></item>
<item><title>Still only</title><link>https://x.test/b</link>
  <pubDate>Wed, 27 Aug 2026 08:00:00 -0400</pubDate>
  <media:content url="https://x.test/b.jpg" type="image/jpeg"/></item>
<item><title>Nothing attached</title><link>https://x.test/c</link>
  <pubDate>Wed, 27 Aug 2026 07:00:00 -0400</pubDate></item>
</channel></rss>"""
with mock.patch.object(F, "get", lambda *a, **k: fake_response(content=MEDIA_RSS)):
    m = F._parse_feed("https://example.com/media")
check("video enclosure found", m[0]["media_type"] == "video" and m[0]["media"].endswith(".mp4"))
check("image falls back to still", m[1]["media_type"] == "image")
check("no media stays null", m[2]["media"] is None and m[2]["media_type"] is None)

# ---------------------------------------------------------------- streams
check("every news section has streams", set(F.STREAMS) == {"local", "colombia", "world"})
check("streams carry name and url",
      all(x.get("name") and x.get("url", "").startswith("https://")
          for v in F.STREAMS.values() for x in v))

# ---------------------------------------------------------------- failure shape
f = F.fail("SomeSource", "boom")
check("failure block has ok False", f["ok"] is False)
check("failure block names the source", f["source"] == "SomeSource")
check("failure block is serialisable", json.dumps(f) and True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
