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
FREE = {"routes": [
    {"summary": {"lengthInMeters": 35000, "travelTimeInSeconds": 2040}},
    {"summary": {"lengthInMeters": 33000, "travelTimeInSeconds": 2400}},
]}
OUTBOUND = F.COMMUTE_LEGS[0]
RETURN = F.COMMUTE_LEGS[1]

# Incidents are collected first (one call per county), then the route, then
# the alternates. Pin the leg so the test does not depend on the wall clock.
seq = [INC, INC_EMPTY, ROUTE, ALTS, FREE]
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
check("best toll-free captured", c["best_free"]["minutes"] == 34)
check("best toll-free saves is computed", c["best_free"]["saves"] == 4)
# ---- Live TV channel list -------------------------------------------
check("every channel has a name and a region",
      all(c.get("name") and c.get("region") for c in F.LIVE_TV))
check("every channel is playable or linkable",
      all(c.get("yt") or c.get("url") for c in F.LIVE_TV))
check("channel ids look like real youtube ids",
      all(c["yt"].startswith("UC") and len(c["yt"]) == 24
          for c in F.LIVE_TV if c.get("yt")))
check("at least one channel streams around the clock",
      any(c.get("always") and c.get("yt") for c in F.LIVE_TV))
check("a link-only channel explains why",
      all(c.get("note") for c in F.LIVE_TV if not c.get("yt")))
check("no channel is both a player and a bare link",
      not [c for c in F.LIVE_TV if c.get("yt") and c.get("url")])
check("no duplicate channel ids",
      len({c["yt"] for c in F.LIVE_TV if c.get("yt")}) ==
      len([c for c in F.LIVE_TV if c.get("yt")]))
check("no duplicate channel names",
      len({c["name"] for c in F.LIVE_TV}) == len(F.LIVE_TV))
check("CNN is gone from live TV",
      not [c for c in F.LIVE_TV if "cnn" in c["name"].lower()])
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
check("worst delay outranks a closure", inc[0]["delay_s"] == 900 and inc[0]["roads"] == ["SR-826"])
check("named closure ranks above ordinary congestion",
      inc[1]["magnitude"] == 4 and inc[1]["roads"] == ["I-595"])

# The real feed is mostly unnamed "Closed" entries with no delay. They must
# never outrank a named road, or the card fills with rows saying nothing.
NOISE = [
    {"county": "Broward", "roads": [], "delay_s": None, "magnitude": 4,
     "description": "Closed", "start": "x"},
    {"county": "Broward", "roads": ["I-95"], "delay_s": 120, "magnitude": 2,
     "description": "Slow", "start": "x"},
]
NOISE.sort(key=lambda i: (bool(i["roads"]), F._cost(i), i.get("delay_s") or 0),
          reverse=True)
check("unnamed closure sinks below a named road", NOISE[0]["roads"] == ["I-95"])
check("major needs a road name",
      F._is_major({"roads": [], "magnitude": 4, "delay_s": None}) is False)
check("closed named road is major",
      F._is_major({"roads": ["I-595"], "magnitude": 4, "delay_s": None}) is True)
check("ten minutes lost is major",
      F._is_major({"roads": ["I-95"], "magnitude": 2, "delay_s": 600}) is True)
check("two minutes lost is not",
      F._is_major({"roads": ["I-95"], "magnitude": 2, "delay_s": 120}) is False)
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

# ---------------------------------------------------------------- world map geometry
sys.path.insert(0, "scripts")
import build_map as M  # noqa: E402

check("west edge projects to x=0", M.project(-180, 0)[0] == 0)
check("east edge projects to full width", M.project(180, 0)[0] == M.W)
check("north pole projects to y=0", M.project(0, 90)[1] == 0)
check("south pole projects to full height", M.project(0, -90)[1] == M.H)
check("null island lands mid-canvas", M.project(0, 0) == (M.W / 2, M.H / 2))

line = [(0, 0), (1, 0.001), (2, 0), (3, 0.002), (4, 0)]
check("collinear points collapse", len(M.simplify(line, 0.35)) == 2)
bend = [(0, 0), (5, 50), (10, 0)]
check("a real corner survives", len(M.simplify(bend, 0.35)) == 3)

big = [[-100, 40], [-100, 60], [-60, 60], [-60, 40], [-100, 40]]
d = M.ring_path(big)
check("a large ring becomes a closed subpath", d.startswith("M") and d.endswith("Z"))
tiny = [[0, 0], [0, 0.05], [0.05, 0.05], [0.05, 0], [0, 0]]
check("a speck island is dropped", M.ring_path(tiny) == "")

GJ = {"features": [
    {"properties": {"ISO_A3": "AAA", "NAME": "Alpha"},
     "geometry": {"type": "Polygon", "coordinates": [big]}},
    {"properties": {"ISO_A3": "-99", "ADM0_A3": "BBB", "NAME": "Beta"},
     "geometry": {"type": "MultiPolygon", "coordinates": [[big]]}},
    {"properties": {"ISO_A3": "CCC", "NAME": "Speck"},
     "geometry": {"type": "Polygon", "coordinates": [tiny]}},
]}
built = M.build(GJ)
check("features become country paths", len(built) == 2)
check("-99 falls back to ADM0_A3", built[1]["iso"] == "BBB")
check("multipolygon is handled", built[1]["d"].count("M") == 1)
check("a country with no drawable ring is omitted",
      "CCC" not in [c["iso"] for c in built])

# ---------------------------------------------------------------- world map matching
NEWS = {"world": [
    {"title": "Russia strikes Kharkiv as Ukraine presses counterattack",
     "url": "https://x.test/1", "published": "a"},
    {"title": "Ukrainian drones hit Moscow refinery", "url": "https://x.test/2",
     "published": "b"},
    {"title": "Sudan aid convoy blocked in Darfur", "url": "https://x.test/3",
     "published": "c"},
], "local": [
    {"title": "Jordan scored 30 points against Georgia on Saturday",
     "url": "https://x.test/4", "published": "d"},
], "colombia": [
    {"title": "Petro anuncia plan para Medellín", "url": "https://x.test/5",
     "published": "e"},
]}
w = F.fetch_world(NEWS)
iso = {c["iso"]: c for c in w["countries"]}
check("world block is ok", w["ok"])
check("ukraine matched twice", iso["UKR"]["count"] == 2)
check("russia matched by name and capital", iso["RUS"]["count"] == 2)
check("sudan matched", "SDN" in iso)
check("colombia matched in spanish", "COL" in iso)
check("a basketball Jordan is not a country", "JOR" not in iso)
check("a US-state Georgia is not a country", "GEO" not in iso)
check("issue is a real headline",
      iso["SDN"]["issue"] == "Sudan aid convoy blocked in Darfur")
check("every story keeps its link",
      all(st["url"] for c in w["countries"] for st in c["stories"]))
check("busiest country sorts first", w["countries"][0]["count"] >= w["countries"][-1]["count"])
check("headline count is reported", w["headlines_scanned"] == 5)

# ---------------------------------------------------------------- global events
GD = b"""<?xml version="1.0"?>
<rss xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"
     xmlns:gdacs="http://www.gdacs.org"><channel>
<item><title>Red alert cyclone DOLLY-26</title><link>https://g.test/1</link>
  <pubDate>Thu, 27 Aug 2026 10:00:00 GMT</pubDate>
  <geo:lat>-18.4</geo:lat><geo:long>47.5</geo:long>
  <gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:eventtype>TC</gdacs:eventtype>
  <gdacs:country>Madagascar</gdacs:country><gdacs:severity>210 km/h</gdacs:severity></item>
<item><title>Green forest fire in Australia</title><link>https://g.test/2</link>
  <pubDate>Thu, 27 Aug 2026 09:00:00 GMT</pubDate>
  <geo:lat>-33.9</geo:lat><geo:long>151.2</geo:long>
  <gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:eventtype>WF</gdacs:eventtype></item>
<item><title>Event with no usable coordinates</title><link>https://g.test/3</link>
  <gdacs:alertlevel>Orange</gdacs:alertlevel><gdacs:eventtype>FL</gdacs:eventtype></item>
<item><title>Event off the edge of the earth</title><link>https://g.test/4</link>
  <geo:lat>999</geo:lat><geo:long>12</geo:long>
  <gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:eventtype>EQ</gdacs:eventtype></item>
</channel></rss>"""
with mock.patch.object(F, "get", lambda *a, **k: fake_response(content=GD)):
    g = F._gdacs_events()
check("gdacs events parsed", len(g) == 2, f"got {len(g)}")
check("alert level lowercased", g[0]["level"] == "red")
check("event code becomes a readable kind", g[0]["kind"] == "cyclone" and g[1]["kind"] == "wildfire")
check("coordinates survive intact", g[0]["lat"] == -18.4 and g[0]["lon"] == 47.5)
check("an event with no point is dropped, not placed at 0,0",
      not [e for e in g if e["lat"] == 0 and e["lon"] == 0])
check("an impossible latitude is dropped", "edge of the earth" not in str(g))

USGS = {"features": [
    {"geometry": {"coordinates": [121.57, 22.84, 10]},
     "properties": {"mag": 7.1, "place": "Taiwan", "url": "https://u.test/1", "time": 1}},
    {"geometry": {"coordinates": [-122.0, 37.5, 8]},
     "properties": {"mag": 4.6, "place": "California", "url": "https://u.test/2", "time": 2}},
    {"geometry": {"coordinates": [1.0]},
     "properties": {"mag": 5.0, "place": "truncated", "url": "x", "time": 3}},
]}
with mock.patch.object(F, "get", lambda *a, **k: fake_response(USGS)):
    u = F._usgs_events()
check("quakes parsed", len(u) == 2)
check("lon/lat are not transposed", u[0]["lat"] == 22.84 and u[0]["lon"] == 121.57)
check("a big quake is red", u[0]["level"] == "red")
check("a moderate quake is green", u[1]["level"] == "green")
check("a truncated geometry is skipped", all(e["place"] != "truncated" for e in u))

seq3 = [fake_response(content=GD), fake_response(USGS)]
with mock.patch.object(F, "get", lambda *a, **k: seq3.pop(0)):
    ev = F.fetch_events()
check("events block ok", ev["ok"] and ev["event_count"] == 4)
check("red events sort to the front", ev["events"][0]["level"] == "red")
check("levels are counted", ev["counts"]["red"] == 2 and ev["counts"]["green"] == 2)
check("both sources named", "GDACS" in ev["source"] and "USGS" in ev["source"])

def _boom(*a, **k):
    raise RuntimeError("network down")
with mock.patch.object(F, "get", _boom):
    ev2 = F.fetch_events()
check("total outage fails loudly", ev2["ok"] is False)
check("failed sources are named", set(ev2["failed_sources"]) == {"GDACS", "USGS"})

# ---------------------------------------------------------------- failure shape
f = F.fail("SomeSource", "boom")
check("failure block has ok False", f["ok"] is False)
check("failure block names the source", f["source"] == "SomeSource")
check("failure block is serialisable", json.dumps(f) and True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
