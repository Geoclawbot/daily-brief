#!/usr/bin/env python3
"""
Daily Brief — data collector.

Runs on a schedule in GitHub Actions, writes data/brief.json, and the app
renders from that file. Nothing here needs a server.

DESIGN RULE, carried over from months of this brief going wrong:
every value is either traceable to a named source with a timestamp, or it is
absent. A field is allowed to be null. A field is never allowed to be a guess.
Each block therefore carries its own `source`, `fetched_at` and `ok`, and the
app shows "not sourced" wherever ok is false rather than reusing yesterday.
"""

import json
import os
import re
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ET = ZoneInfo("America/New_York")
HOME = (25.971228, -80.362648)      # lat, lon
JACKSON = (25.7922591, -80.2132758)

# Traffic incidents are collected county-wide, not just along the commute.
# Two boxes rather than one wide one: TomTom does not return coordinates with
# the fields we ask for, so asking per county is the only honest way to say
# where an incident actually is.
#                minLon,  minLat,  maxLon,  maxLat
COUNTY_BBOX = {
    "Broward":    (-80.50, 25.96, -80.05, 26.34),
    "Miami-Dade": (-80.62, 25.34, -80.10, 25.97),
}
INCIDENT_FIELDS = ("{incidents{type,properties{iconCategory,magnitudeOfDelay,"
                   "events{description},startTime,endTime,delay,roadNumbers}}}")

# The two drives that actually matter, and the hours either one is worth
# routing. Outside these windows the card drops the route and shows area
# traffic only — a travel time for a trip nobody is about to take is noise.
COMMUTE_LEGS = [
    {"key": "outbound", "label": "Home \u2192 Jackson Memorial", "clock": "5:45 AM",
     "origin": HOME, "dest": JACKSON, "hour": 5, "minute": 45, "window": (3, 9)},
    {"key": "return", "label": "Jackson Memorial \u2192 Home", "clock": "3:30 PM",
     "origin": JACKSON, "dest": HOME, "hour": 15, "minute": 30, "window": (12, 18)},
]

# Live streams for the news sections. Static on purpose: these are station
# landing pages, not scraped URLs that can rot without anyone noticing.
# Channels for the Live TV card, with channel IDs verified against each
# channel's own page rather than taken from a search result.
#
# always=True means a genuine round-the-clock stream — those play whenever you
# tap them. always=False means the channel only goes live for its newscasts,
# so most of the day YouTube will show its own "offline" card. That is the
# honest state, not a bug, and the card says so.
#
# CNN has no free live stream on YouTube at all — its live TV sits behind a
# cable login — so it is a link, not a player. Better an outbound link than a
# black rectangle pretending to be a feed.
LIVE_TV = [
    # Round-the-clock streams. These play whenever you tap them.
    {"name": "Sky News", "region": "World", "always": True,
     "yt": "UCoMdktPbSTixAyNGwb-UYkQ"},
    {"name": "Al Jazeera English", "region": "World", "always": True,
     "yt": "UCNye-wNBqNL5ZzHSJj3l8Bg"},
    {"name": "DW News", "region": "World", "always": True,
     "yt": "UCknLrEdhRCp1aegoMqRaCZg"},
    {"name": "FRANCE 24 English", "region": "World", "always": True,
     "yt": "UCQfwfsi5VrQ8yKZ-UWmAEFg"},
    {"name": "euronews", "region": "World", "always": True,
     "yt": "UCSrZ3UV4jOidv8ppoVuvW9Q"},
    {"name": "Bloomberg TV", "region": "Business", "always": True,
     "yt": "UCIALMKvObZNtJ6AmdCLP7Lg"},
    {"name": "El Tiempo", "region": "Colombia", "always": True,
     "yt": "UCe5-b0fCK3eQCpwS6MT0aNw"},

    # On air only at certain hours — newscasts, market sessions, breaking events.
    {"name": "CNBC", "region": "Business", "always": False,
     "yt": "UCvJJ_dzjViJCoLf5uKUTwoA"},
    {"name": "WPLG Local 10", "region": "Local", "always": False,
     "yt": "UCgVZ0mrM3liHNhRYC5Mchgg"},
    {"name": "WSVN 7News", "region": "Local", "always": False,
     "yt": "UC0IyiKpx7Oirfbqelu3WFJA"},
    {"name": "Kyiv Independent", "region": "World", "always": False,
     "yt": "UCGAC5yzlYgjKoJABDZ7zEyw"},
]

STREAMS = {
    "local": [
        {"name": "WPLG Local 10", "url": "https://www.local10.com/live/", "live": True},
        {"name": "WPLG on YouTube", "url": "https://www.youtube.com/@WPLGLocal10/streams", "live": True},
        {"name": "WSVN 7News", "url": "https://wsvn.com/on-air-live-stream/", "live": True},
    ],
    "colombia": [
        {"name": "El Tiempo video", "url": "https://www.eltiempo.com/videos", "live": False},
    ],
    "world": [
        {"name": "Kyiv Independent", "url": "https://www.youtube.com/@kyivindependent/streams", "live": True},
        {"name": "NPR program stream", "url": "https://www.npr.org/about-npr/472557877/npr-program-stream", "live": True},
    ],
}
UA = {"User-Agent": "daily-brief/1.0 (personal morning brief; contact via repo issues)"}
TIMEOUT = 25

TOMTOM_KEY = os.environ.get("TOMTOM_API_KEY", "").strip()
AIRNOW_KEY = os.environ.get("AIRNOW_API_KEY", "").strip()


def now_et():
    return dt.datetime.now(ET)


def stamp():
    return now_et().isoformat(timespec="seconds")


def block(source, ok=True, **fields):
    """Every data block looks the same so the app can trust its shape."""
    out = {"ok": ok, "source": source, "fetched_at": stamp()}
    out.update(fields)
    return out


def fail(source, why):
    print(f"  !! {source}: {why}", file=sys.stderr)
    return block(source, ok=False, error=str(why)[:300])


def get(url, headers=None, **kw):
    h = dict(UA)
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# WEATHER — Open-Meteo. No key. Returns ISO8601 stamps in the timezone you ask
# for, which is the whole reason the heat curve can exist here and could not
# exist in the scraped version: third-party HTML pages rendered the *fetcher's*
# clock and labelled it EDT. This does not.
# --------------------------------------------------------------------------
def fetch_weather():
    try:
        r = get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": HOME[0],
                "longitude": HOME[1],
                "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability",
                "daily": "temperature_2m_max,temperature_2m_min,apparent_temperature_max,precipitation_probability_max",
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "America/New_York",
                "forecast_days": 3,
            },
        )
        d = r.json()
        h = d["hourly"]

        # Keep only today, 5 AM to 9 PM — the window the curve plots.
        today = now_et().date().isoformat()
        curve = []
        for i, t in enumerate(h["time"]):
            if not t.startswith(today):
                continue
            hour = int(t[11:13])
            if hour < 5 or hour > 21:
                continue
            curve.append({
                "time": t,
                "hour": hour,
                "temp": h["temperature_2m"][i],
                "feels": h["apparent_temperature"][i],
                "humidity": h["relative_humidity_2m"][i],
                "pop": h["precipitation_probability"][i],
            })

        cur = d.get("current", {})
        day = d.get("daily", {})
        return block(
            "Open-Meteo (ECMWF/GFS blend)",
            curve=curve,
            current={
                "temp": cur.get("temperature_2m"),
                "feels": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "wind_mph": cur.get("wind_speed_10m"),
                "wind_dir": cur.get("wind_direction_10m"),
                "observed_at": cur.get("time"),
            },
            today={
                "high": day.get("temperature_2m_max", [None])[0],
                "low": day.get("temperature_2m_min", [None])[0],
                "feels_max": day.get("apparent_temperature_max", [None])[0],
                "pop_max": day.get("precipitation_probability_max", [None])[0],
            },
            peak_feels=max((c["feels"] for c in curve), default=None),
            peak_hour=max(curve, key=lambda c: c["feels"])["hour"] if curve else None,
        )
    except Exception as e:
        return fail("Open-Meteo", e)


# --------------------------------------------------------------------------
# ALERTS — api.weather.gov. No key. Robots blocked my browsing tool, not a
# server-side client; this is the documented, intended way to use it.
# --------------------------------------------------------------------------
def fetch_alerts():
    try:
        r = get(
            "https://api.weather.gov/alerts/active",
            params={"point": f"{HOME[0]},{HOME[1]}"},
            headers={"Accept": "application/geo+json"},
        )
        feats = r.json().get("features", [])
        alerts = []
        for f in feats:
            p = f.get("properties", {})
            alerts.append({
                "event": p.get("event"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "headline": p.get("headline"),
                "description": (p.get("description") or "")[:900],
                "onset": p.get("onset"),
                "ends": p.get("ends") or p.get("expires"),
            })
        return block("NWS api.weather.gov/alerts", alerts=alerts, count=len(alerts))
    except Exception as e:
        return fail("NWS alerts", e)


# --------------------------------------------------------------------------
# AIR QUALITY — AirNow gives a real AQI integer plus a forecast. Falls back to
# Open-Meteo's air-quality endpoint (keyless) so this section still works
# before you add the key.
# --------------------------------------------------------------------------
def fetch_air():
    if AIRNOW_KEY:
        try:
            obs = get(
                "https://www.airnowapi.org/aq/observation/latLong/current/",
                params={"format": "application/json", "latitude": HOME[0],
                        "longitude": HOME[1], "distance": 40, "API_KEY": AIRNOW_KEY},
            ).json()
            fc = get(
                "https://www.airnowapi.org/aq/forecast/latLong/",
                params={"format": "application/json", "latitude": HOME[0],
                        "longitude": HOME[1], "distance": 40, "API_KEY": AIRNOW_KEY},
            ).json()
            if obs:
                worst = max(obs, key=lambda o: o.get("AQI", 0))
                return block(
                    "AirNow (EPA)",
                    aqi=worst.get("AQI"),
                    category=worst.get("Category", {}).get("Name"),
                    pollutant=worst.get("ParameterName"),
                    reporting_area=worst.get("ReportingArea"),
                    observed=f'{worst.get("DateObserved","").strip()} {worst.get("HourObserved","")}:00 {worst.get("LocalTimeZone","")}',
                    forecast=[{"date": f.get("DateForecast", "").strip(),
                               "aqi": f.get("AQI"),
                               "category": f.get("Category", {}).get("Name"),
                               "pollutant": f.get("ParameterName")} for f in fc],
                )
        except Exception as e:
            print(f"  .. AirNow failed, falling back: {e}", file=sys.stderr)

    try:
        d = get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": HOME[0], "longitude": HOME[1],
                    "current": "us_aqi,pm2_5,pm10", "timezone": "America/New_York"},
        ).json()
        c = d.get("current", {})
        aqi = c.get("us_aqi")
        cats = [(50, "Good"), (100, "Moderate"), (150, "Unhealthy for Sensitive Groups"),
                (200, "Unhealthy"), (300, "Very Unhealthy"), (10**9, "Hazardous")]
        cat = next((n for t, n in cats if aqi is not None and aqi <= t), None)
        return block("Open-Meteo air quality (no key)", aqi=aqi, category=cat,
                     pm25=c.get("pm2_5"), pm10=c.get("pm10"),
                     observed=c.get("time"), forecast=[],
                     note="Add AIRNOW_API_KEY for EPA observations and a multi-day forecast.")
    except Exception as e:
        return fail("Air quality", e)


# --------------------------------------------------------------------------
# COMMUTE — TomTom. The key never expired; the Claude connector kept dropping
# it. Called directly over HTTPS it is stable.
# --------------------------------------------------------------------------
def _active_leg(n=None):
    """Which drive, if any, is worth routing right now. None on weekends."""
    n = n or now_et()
    if n.weekday() >= 5:
        return None
    for leg in COMMUTE_LEGS:
        lo, hi = leg["window"]
        if lo <= n.hour < hi:
            return leg
    return None


def _depart_at(leg=None, n=None):
    """The leg's departure time, as TomTom wants it.

    Inside the window but past the clock — you are already driving, or about
    to — ask for live conditions instead of a stale scheduled time. Outside
    it, the next occurrence.
    """
    leg = leg or COMMUTE_LEGS[0]
    n = n or now_et()
    d = n.replace(hour=leg["hour"], minute=leg["minute"], second=0, microsecond=0)
    if n >= d:
        lo, hi = leg["window"]
        if lo <= n.hour < hi:
            return "now"
        d += dt.timedelta(days=1)
    return d.isoformat(timespec="seconds")


def _route(leg, avoid_tolls, depart, alternatives=0):
    o, dst = leg["origin"], leg["dest"]
    loc = f"{o[0]},{o[1]}:{dst[0]},{dst[1]}"
    params = {
        "key": TOMTOM_KEY,
        "traffic": "true",
        "departAt": depart,
        "sectionType": "toll",
        "travelMode": "car",
    }
    if avoid_tolls:
        params["avoid"] = "tollRoads"
    if alternatives:
        params["maxAlternatives"] = alternatives
        params["alternativeType"] = "anyRoute"
    else:
        params["routeRepresentation"] = "summaryOnly"
    r = get(f"https://api.tomtom.com/routing/1/calculateRoute/{loc}/json", params=params)
    return r.json()


def _area_incidents():
    """Every incident across Miami-Dade and Broward, worst first.

    Sorted so a full closure outranks any delay, then by delay length. The
    commute route is still fetched separately; this is the wider picture for
    days when the drive is not the 5:45 run to Jackson.
    """
    out, seen = [], set()
    for county, (w, s_, e, n) in COUNTY_BBOX.items():
        try:
            r = get(
                "https://api.tomtom.com/traffic/services/5/incidentDetails",
                params={"key": TOMTOM_KEY, "bbox": f"{w},{s_},{e},{n}",
                        "fields": INCIDENT_FIELDS, "language": "en-GB"},
            ).json()
        except Exception as ex:
            print(f"  .. incidents {county}: {ex}", file=sys.stderr)
            continue
        for it in r.get("incidents", []):
            p = it.get("properties", {})
            roads = p.get("roadNumbers") or []
            desc = "; ".join(ev.get("description", "") for ev in p.get("events", []))
            key = (desc, tuple(roads), p.get("startTime"))
            if key in seen:          # the two boxes touch; do not double-count
                continue
            seen.add(key)
            out.append({
                "county": county,
                "roads": roads,
                "delay_s": p.get("delay"),
                "magnitude": p.get("magnitudeOfDelay"),
                "description": desc,
                "start": p.get("startTime"),
            })
    # Tie-break on the measured delay: a closure's score is an assumption we
    # made, a delay is a number TomTom actually reported. Real data wins.
    out.sort(key=lambda i: (bool(i["roads"]), _cost(i), i.get("delay_s") or 0),
             reverse=True)
    return out


def _cost(i):
    """Roughly what an incident costs you, in seconds, for ranking only.

    A closure carries no delay figure, so it would sink to the bottom of a
    pure delay sort even though it is the thing you most need to know. Score
    it as a quarter hour: enough to outrank ordinary congestion, not enough to
    bury a genuinely bad jam on the interstate.
    """
    if (i.get("magnitude") or 0) == 4:
        return 900
    return i.get("delay_s") or 0


def _is_major(i):
    """Worth calling out: a closed named road, or ten minutes lost."""
    if not i["roads"]:
        return False
    return (i.get("magnitude") or 0) == 4 or (i.get("delay_s") or 0) >= 600


def fetch_commute():
    if not TOMTOM_KEY:
        return fail("TomTom", "TOMTOM_API_KEY not set — add it as a repository secret")
    try:
        incidents = _area_incidents()
        majors = [i for i in incidents if _is_major(i)]
        named = [i for i in incidents if i["roads"]]
        area = dict(incidents=incidents[:30], incident_count=len(incidents),
                    named_count=len(named), major_count=len(majors),
                    coverage="Miami-Dade + Broward")

        leg = _active_leg()
        if leg is None:
            # Between the two runs, or a weekend. Area traffic still matters.
            return block("TomTom Traffic", route_shown=False, leg=None,
                         direction=None, **area)

        depart = _depart_at(leg)
        mine = _route(leg, True, depart)["routes"][0]["summary"]
        miles = round(mine["lengthInMeters"] / 1609.34, 1)
        mins = round(mine["travelTimeInSeconds"] / 60)
        delay = round(mine.get("trafficDelayInSeconds", 0) / 60)

        alts = []
        try:
            for rt in _route(leg, False, depart, alternatives=3).get("routes", []):
                sm = rt["summary"]
                am = round(sm["lengthInMeters"] / 1609.34, 1)
                at = round(sm["travelTimeInSeconds"] / 60)
                alts.append({"miles": am, "minutes": at,
                             "toll": bool(rt.get("sections")), "saves": mins - at})
        except Exception as e:
            print(f"  .. alternates failed: {e}", file=sys.stderr)

        # His rule: only surface a tolled alternate that beats the default by
        # >10 min. Tolls have to earn their place.
        worth = [a for a in alts if a["saves"] > 10]

        # Separately: the quickest way home that costs nothing. The default
        # route already avoids tolls, but TomTom's alternates sometimes find a
        # faster toll-free line, and that is the one he actually wants.
        free_alts = []
        try:
            for rt in _route(leg, True, depart, alternatives=3).get("routes", []):
                sm = rt["summary"]
                free_alts.append({
                    "miles": round(sm["lengthInMeters"] / 1609.34, 1),
                    "minutes": round(sm["travelTimeInSeconds"] / 60),
                    "delay_minutes": round(sm.get("trafficDelayInSeconds", 0) / 60),
                })
        except Exception as e:
            print(f"  .. toll-free alternates failed: {e}", file=sys.stderr)

        best_free = min(free_alts, key=lambda a: a["minutes"]) if free_alts else None
        if best_free:
            best_free["saves"] = mins - best_free["minutes"]

        return block("TomTom Routing + Traffic", route_shown=True,
                     leg=leg["label"], direction=leg["key"], depart_clock=leg["clock"],
                     depart_at=depart, miles=miles, minutes=mins,
                     delay_minutes=delay, toll_free=True, alternates=alts,
                     alternates_worth_taking=worth, free_alternates=free_alts,
                     best_free=best_free, **area)
    except Exception as e:
        return fail("TomTom", e)


# --------------------------------------------------------------------------
# ROAD CLOSURES — FDOT District 6 and FL511. Keyless, and they cover the thing
# TomTom does not: multi-week construction on the Okeechobee/Palmetto stretch.
# --------------------------------------------------------------------------
def fetch_closures():
    out, ok = [], False
    try:
        s = BeautifulSoup(get("https://www.fdotmiamidade.com/laneclosures").text, "html.parser")
        text = re.sub(r"\s+", " ", s.get_text(" "))
        for m in re.finditer(r"([^.]*?(?:SR-826|Palmetto|Okeechobee|US-27|NW 36|I-75|NW 12)[^.]*\.)", text):
            frag = m.group(1).strip()
            if 40 < len(frag) < 400:
                out.append({"source": "FDOT District 6", "text": frag})
        ok = True
    except Exception as e:
        print(f"  .. FDOT failed: {e}", file=sys.stderr)

    alerts = []
    try:
        s = BeautifulSoup(get("https://fl511.com/List/Alerts").text, "html.parser")
        for tr in s.select("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ")).strip() for td in tr.select("td")]
            if len(cells) >= 2 and any(cells):
                alerts.append(" | ".join(c for c in cells if c))
        ok = True
    except Exception as e:
        print(f"  .. FL511 failed: {e}", file=sys.stderr)

    local = [a for a in alerts if re.search(r"Miami-Dade|Broward|Southeast", a, re.I)]
    return block("FDOT District 6 + FL511", ok=ok, closures=out[:8],
                 fl511_all=alerts[:20], fl511_local=local)


# --------------------------------------------------------------------------
# TROPICS — NHC public text product. Keyless.
# --------------------------------------------------------------------------
def fetch_tropics():
    try:
        s = BeautifulSoup(get("https://www.nhc.noaa.gov/text/MIATWOAT.shtml").text, "html.parser")
        pre = s.find("pre")
        raw = pre.get_text() if pre else s.get_text()
        raw = re.sub(r"\n{3,}", "\n\n", raw.strip())
        issued = None
        m = re.search(r"(\d{3,4}\s+(?:AM|PM)\s+\w+\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{4})", raw)
        if m:
            issued = m.group(1)
        areas = []
        for para in re.split(r"\n\s*\n", raw):
            pcts = re.findall(r"(\d{1,3})\s*percent", para)
            if pcts:
                head = re.sub(r"\s+", " ", para.strip())[:320]
                areas.append({"text": head,
                              "chance_48h": int(pcts[0]),
                              "chance_7d": int(pcts[-1])})
        return block("NOAA NHC MIATWOAT", issued=issued, areas=areas, raw=raw[:2500])
    except Exception as e:
        return fail("NHC", e)


# --------------------------------------------------------------------------
# FUEL — AAA state averages. Keyless scrape.
# --------------------------------------------------------------------------
def fetch_fuel():
    try:
        s = BeautifulSoup(get("https://gasprices.aaa.com/state-gas-price-averages/").text, "html.parser")
        florida = national = diesel = None
        for tr in s.select("tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td,th")]
            if not cells:
                continue
            label = cells[0].lower()
            prices = [c for c in cells[1:] if re.match(r"^\$\d", c)]
            if label.startswith("florida") and prices:
                florida = prices[0]
                if len(prices) >= 4:
                    diesel = prices[-1]
        m = re.search(r"National Average[^$]*(\$\d\.\d+)", s.get_text(" "))
        if m:
            national = m.group(1)
        as_of = None
        m2 = re.search(r"as of\s*(\d{1,2}/\d{1,2}/\d{2,4})", s.get_text(" "), re.I)
        if m2:
            as_of = m2.group(1)
        return block("AAA state gas price averages",
                     ok=bool(florida), florida=florida, national=national,
                     diesel=diesel, as_of=as_of)
    except Exception as e:
        return fail("AAA", e)


# --------------------------------------------------------------------------
# NEWS — RSS only. Feeds carry real pubDates, which is what stopped the brief
# reporting a June wildfire story as though it were August.
# --------------------------------------------------------------------------
FEEDS = {
    "local":    ["https://www.local10.com/arc/outboundfeeds/rss/category/news/local/?outputType=xml",
                 "https://wsvn.com/news/local/feed/"],
    "colombia": ["https://www.eltiempo.com/rss/colombia.xml"],
    "world":    ["https://kyivindependent.com/feed/",
                 "https://feeds.npr.org/1004/rss.xml"],
}


def _media(it):
    """Pull a video or image out of an RSS item if the feed offers one.

    Feeds advertise media through <enclosure> or the media: namespace. We
    prefer video and fall back to a still. Absent means absent — no guessing
    a thumbnail URL from the article link.
    """
    vid = img = None
    for tag in it.find_all(["enclosure", "content", "thumbnail"]):
        u = tag.get("url")
        if not u:
            continue
        t = (tag.get("type") or "").lower()
        low = u.lower()
        if t.startswith("video") or low.endswith((".mp4", ".m3u8", ".mov")):
            vid = vid or u
        elif t.startswith("image") or low.endswith((".jpg", ".jpeg", ".png", ".webp")):
            img = img or u
    if vid:
        return vid, "video"
    if img:
        return img, "image"
    return None, None


def _parse_feed(url, limit=8):
    items = []
    try:
        s = BeautifulSoup(get(url).content, "xml")
        for it in s.find_all(["item", "entry"])[:limit]:
            title = it.find("title")
            link = it.find("link")
            date = it.find("pubDate") or it.find("published") or it.find("updated")
            href = link.get("href") if (link and link.get("href")) else (link.text if link else None)
            media, kind = _media(it)
            items.append({
                "title": re.sub(r"\s+", " ", title.text).strip() if title else None,
                "url": href,
                "published": date.text.strip() if date else None,
                "feed": url,
                "media": media,
                "media_type": kind,
            })
    except Exception as e:
        print(f"  .. feed {url}: {e}", file=sys.stderr)
    return items


def fetch_news():
    out = {}
    for section, urls in FEEDS.items():
        items = []
        for u in urls:
            items += _parse_feed(u)
        out[section] = items
    ok = any(v for v in out.values())
    return block("RSS (Local 10, WSVN, El Tiempo, Kyiv Independent, NPR)",
                 ok=ok, streams=STREAMS, live_tv=LIVE_TV, **out)


# --------------------------------------------------------------------------
# WILDFIRES — the section the brief missed entirely for a week.
# --------------------------------------------------------------------------
def fetch_fires():
    items = _parse_feed("https://www.local10.com/arc/outboundfeeds/rss/category/news/florida/?outputType=xml", 25)
    hits = [i for i in items if i.get("title") and
            re.search(r"wildfire|brush fire|fire burn|smoke|acres", i["title"], re.I)]
    return block("Local 10 Florida feed (keyword filter)", ok=True,
                 items=hits[:6], scanned=len(items),
                 note="Headline-level only. Acreage and containment need the article.")


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# WORLD MAP — which countries are in today's headlines.
#
# Derived entirely from the feeds already collected. Nothing is asserted about
# a country unless a headline we fetched named it, and clicking through goes to
# that headline. No conflict list to curate and go stale.
#
# Ambiguous names are matched by demonym or capital rather than the bare word,
# because "Georgia", "Jordan", "Chad" and "Turkey" all mean something else far
# more often in an English news feed than they mean the country.
# --------------------------------------------------------------------------
COUNTRIES = {
    "UKR": ("Ukraine", ["ukraine", "ukrainian", "ukrainians", "kyiv", "kiev"]),
    "RUS": ("Russia", ["russia", "russian", "russians", "moscow", "kremlin", "putin"]),
    "USA": ("United States", ["united states", "u.s.", "american", "washington",
                              "white house", "pentagon", "congress"]),
    "COL": ("Colombia", ["colombia", "colombian", "bogota", "bogotá", "medellin",
                         "medellín", "cali", "petro"]),
    "VEN": ("Venezuela", ["venezuela", "venezuelan", "caracas", "maduro"]),
    "ISR": ("Israel", ["israel", "israeli", "israelis", "tel aviv", "netanyahu"]),
    "PSE": ("Palestine", ["palestine", "palestinian", "palestinians", "gaza", "west bank"]),
    "IRN": ("Iran", ["iran", "iranian", "iranians", "tehran"]),
    "IRQ": ("Iraq", ["iraq", "iraqi", "baghdad"]),
    "SYR": ("Syria", ["syria", "syrian", "damascus"]),
    "LBN": ("Lebanon", ["lebanon", "lebanese", "beirut", "hezbollah"]),
    "YEM": ("Yemen", ["yemen", "yemeni", "houthi", "houthis"]),
    "SAU": ("Saudi Arabia", ["saudi arabia", "saudi", "saudis", "riyadh"]),
    "ARE": ("United Arab Emirates", ["united arab emirates", "emirati", "abu dhabi", "dubai"]),
    "QAT": ("Qatar", ["qatar", "qatari", "doha"]),
    "EGY": ("Egypt", ["egypt", "egyptian", "cairo"]),
    "TUR": ("Turkey", ["turkish", "türkiye", "turkiye", "ankara", "istanbul", "erdogan"]),
    "CHN": ("China", ["china", "chinese", "beijing", "shanghai", "xi jinping"]),
    "TWN": ("Taiwan", ["taiwan", "taiwanese", "taipei"]),
    "JPN": ("Japan", ["japan", "japanese", "tokyo"]),
    "KOR": ("South Korea", ["south korea", "south korean", "seoul"]),
    "PRK": ("North Korea", ["north korea", "north korean", "pyongyang"]),
    "IND": ("India", ["india", "indian", "new delhi", "mumbai", "modi"]),
    "PAK": ("Pakistan", ["pakistan", "pakistani", "islamabad"]),
    "AFG": ("Afghanistan", ["afghanistan", "afghan", "kabul", "taliban"]),
    "BGD": ("Bangladesh", ["bangladesh", "bangladeshi", "dhaka"]),
    "MMR": ("Myanmar", ["myanmar", "burmese", "burma", "yangon"]),
    "THA": ("Thailand", ["thailand", "thai", "bangkok"]),
    "VNM": ("Vietnam", ["vietnam", "vietnamese", "hanoi"]),
    "PHL": ("Philippines", ["philippines", "filipino", "manila"]),
    "IDN": ("Indonesia", ["indonesia", "indonesian", "jakarta"]),
    "AUS": ("Australia", ["australia", "australian", "canberra", "sydney"]),
    "NZL": ("New Zealand", ["new zealand", "wellington", "auckland"]),
    "GBR": ("United Kingdom", ["united kingdom", "britain", "british", "england",
                               "london", "scotland", "wales"]),
    "IRL": ("Ireland", ["ireland", "irish", "dublin"]),
    "FRA": ("France", ["france", "french", "paris", "macron"]),
    "DEU": ("Germany", ["germany", "german", "berlin"]),
    "ITA": ("Italy", ["italy", "italian", "rome"]),
    "ESP": ("Spain", ["spain", "spanish", "madrid", "barcelona"]),
    "PRT": ("Portugal", ["portugal", "portuguese", "lisbon"]),
    "NLD": ("Netherlands", ["netherlands", "dutch", "amsterdam", "the hague"]),
    "BEL": ("Belgium", ["belgium", "belgian", "brussels"]),
    "CHE": ("Switzerland", ["switzerland", "swiss", "geneva", "zurich"]),
    "AUT": ("Austria", ["austria", "austrian", "vienna"]),
    "POL": ("Poland", ["poland", "polish", "warsaw"]),
    "SWE": ("Sweden", ["sweden", "swedish", "stockholm"]),
    "NOR": ("Norway", ["norway", "norwegian", "oslo"]),
    "FIN": ("Finland", ["finland", "finnish", "helsinki"]),
    "DNK": ("Denmark", ["denmark", "danish", "copenhagen"]),
    "GRC": ("Greece", ["greece", "greek", "athens"]),
    "HUN": ("Hungary", ["hungary", "hungarian", "budapest", "orban", "orbán"]),
    "ROU": ("Romania", ["romania", "romanian", "bucharest"]),
    "CZE": ("Czechia", ["czech", "czechia", "prague"]),
    "SRB": ("Serbia", ["serbia", "serbian", "belgrade"]),
    "BLR": ("Belarus", ["belarus", "belarusian", "minsk", "lukashenko"]),
    "MDA": ("Moldova", ["moldova", "moldovan", "chisinau"]),
    "GEO": ("Georgia", ["tbilisi", "georgian government", "republic of georgia"]),
    "ARM": ("Armenia", ["armenia", "armenian", "yerevan"]),
    "AZE": ("Azerbaijan", ["azerbaijan", "azerbaijani", "baku"]),
    "KAZ": ("Kazakhstan", ["kazakhstan", "kazakh", "astana"]),
    "CAN": ("Canada", ["canada", "canadian", "ottawa", "toronto"]),
    "MEX": ("Mexico", ["mexico", "mexican", "mexico city"]),
    "GTM": ("Guatemala", ["guatemala", "guatemalan"]),
    "HND": ("Honduras", ["honduras", "honduran", "tegucigalpa"]),
    "SLV": ("El Salvador", ["el salvador", "salvadoran", "bukele"]),
    "NIC": ("Nicaragua", ["nicaragua", "nicaraguan", "managua"]),
    "CRI": ("Costa Rica", ["costa rica", "costa rican", "san jose"]),
    "PAN": ("Panama", ["panama", "panamanian"]),
    "CUB": ("Cuba", ["cuba", "cuban", "havana", "habana"]),
    "HTI": ("Haiti", ["haiti", "haitian", "port-au-prince"]),
    "DOM": ("Dominican Republic", ["dominican republic", "santo domingo"]),
    "JAM": ("Jamaica", ["jamaica", "jamaican", "kingston"]),
    "PRI": ("Puerto Rico", ["puerto rico", "puerto rican", "san juan"]),
    "BRA": ("Brazil", ["brazil", "brazilian", "brasilia", "sao paulo", "são paulo", "lula"]),
    "ARG": ("Argentina", ["argentina", "argentine", "buenos aires", "milei"]),
    "CHL": ("Chile", ["chile", "chilean", "santiago"]),
    "PER": ("Peru", ["peru", "peruvian", "lima"]),
    "ECU": ("Ecuador", ["ecuador", "ecuadorian", "quito", "guayaquil"]),
    "BOL": ("Bolivia", ["bolivia", "bolivian", "la paz"]),
    "PRY": ("Paraguay", ["paraguay", "paraguayan", "asuncion"]),
    "URY": ("Uruguay", ["uruguay", "uruguayan", "montevideo"]),
    "NGA": ("Nigeria", ["nigeria", "nigerian", "abuja", "lagos"]),
    "NER": ("Niger", ["niger", "nigerien", "niamey"]),
    "MLI": ("Mali", ["mali", "malian", "bamako"]),
    "BFA": ("Burkina Faso", ["burkina faso", "burkinabe", "ouagadougou"]),
    "SDN": ("Sudan", ["sudan", "sudanese", "khartoum", "darfur"]),
    "SSD": ("South Sudan", ["south sudan", "juba"]),
    "ETH": ("Ethiopia", ["ethiopia", "ethiopian", "addis ababa", "tigray"]),
    "SOM": ("Somalia", ["somalia", "somali", "mogadishu", "al-shabaab"]),
    "KEN": ("Kenya", ["kenya", "kenyan", "nairobi"]),
    "TZA": ("Tanzania", ["tanzania", "tanzanian", "dodoma"]),
    "UGA": ("Uganda", ["uganda", "ugandan", "kampala"]),
    "RWA": ("Rwanda", ["rwanda", "rwandan", "kigali"]),
    "COD": ("DR Congo", ["democratic republic of congo", "dr congo", "drc", "kinshasa"]),
    "ZAF": ("South Africa", ["south africa", "south african", "johannesburg", "pretoria"]),
    "ZWE": ("Zimbabwe", ["zimbabwe", "zimbabwean", "harare"]),
    "MOZ": ("Mozambique", ["mozambique", "mozambican", "maputo"]),
    "AGO": ("Angola", ["angola", "angolan", "luanda"]),
    "LBY": ("Libya", ["libya", "libyan", "tripoli"]),
    "TUN": ("Tunisia", ["tunisia", "tunisian", "tunis"]),
    "DZA": ("Algeria", ["algeria", "algerian", "algiers"]),
    "MAR": ("Morocco", ["morocco", "moroccan", "rabat", "casablanca"]),
}

_COUNTRY_RE = {
    iso: re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)
    for iso, (_, terms) in COUNTRIES.items()
}


def fetch_world(news):
    """Countries named in the headlines we already fetched.

    Not a conflict index. It says "these places were in today's news, here is
    what was said, here is the article" — which is a claim the feeds actually
    support. Anything stronger would be us inventing a severity we cannot
    source.
    """
    try:
        items = []
        for section in ("local", "colombia", "world"):
            for it in (news.get(section) or []):
                items.append((section, it))

        found = {}
        for section, it in items:
            title = it.get("title") or ""
            if not title:
                continue
            for iso, rx in _COUNTRY_RE.items():
                if rx.search(title):
                    e = found.setdefault(iso, {"iso": iso, "name": COUNTRIES[iso][0],
                                               "stories": []})
                    e["stories"].append({
                        "title": title,
                        "url": it.get("url"),
                        "published": it.get("published"),
                        "section": section,
                    })

        countries = []
        for e in found.values():
            e["count"] = len(e["stories"])
            e["issue"] = e["stories"][0]["title"]      # what the map shows on hover
            e["stories"] = e["stories"][:6]
            countries.append(e)
        countries.sort(key=lambda c: (-c["count"], c["name"]))

        return block("Derived from the RSS headlines in this brief",
                     countries=countries, headlines_scanned=len(items),
                     countries_matched=len(countries),
                     note="A country appears here because a headline named it, "
                          "not because anyone graded its importance.")
    except Exception as e:
        return fail("World map", e)


# --------------------------------------------------------------------------
# GLOBAL EVENTS — real, geolocated, free to fetch, every one traceable.
#
# What this deliberately is NOT: an instability index, a threat score, a
# posture rating. Those need curated or paid intelligence feeds. Inventing
# them would put a confident number on the screen with nothing behind it,
# which is the one thing this brief refuses to do. Everything here is an
# event somebody official recorded, with its own link back.
# --------------------------------------------------------------------------
GDACS_RSS = "https://www.gdacs.org/xml/rss.xml"
USGS_QUAKES = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
               "summary/4.5_day.geojson")

# GDACS event type codes -> what to call them on the map.
GDACS_KIND = {"EQ": "earthquake", "TC": "cyclone", "FL": "flood",
              "VO": "volcano", "DR": "drought", "WF": "wildfire"}


def _txt(node, *names):
    for n in names:
        t = node.find(n)
        if t and t.text and t.text.strip():
            return t.text.strip()
    return None


def _gdacs_point(it):
    """GDACS publishes coordinates two ways. Accept either, trust neither
    blindly — a point that will not parse is dropped, not defaulted to 0,0,
    which is a real place in the Atlantic."""
    lat, lon = _txt(it, "lat"), _txt(it, "long")
    if lat is None or lon is None:
        p = _txt(it, "point")
        if p and len(p.split()) == 2:
            lat, lon = p.split()
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    return la, lo


def _gdacs_events():
    out = []
    soup = BeautifulSoup(get(GDACS_RSS).content, "xml")
    for it in soup.find_all("item"):
        pt = _gdacs_point(it)
        if not pt:
            continue
        level = (_txt(it, "alertlevel") or "green").lower()
        code = (_txt(it, "eventtype") or "").upper()
        link = _txt(it, "link")
        out.append({
            "lat": pt[0], "lon": pt[1],
            "kind": GDACS_KIND.get(code, "event"),
            "level": level if level in ("red", "orange", "green") else "green",
            "title": re.sub(r"\s+", " ", _txt(it, "title") or "").strip(),
            "place": _txt(it, "country"),
            "severity": _txt(it, "severity"),
            "url": link,
            "when": _txt(it, "pubDate"),
            "source": "GDACS",
        })
    return out


def _usgs_events():
    j = get(USGS_QUAKES).json()
    out = []
    for f in j.get("features", []):
        c = ((f.get("geometry") or {}).get("coordinates") or [])
        if len(c) < 2:
            continue
        p = f.get("properties") or {}
        mag = p.get("mag")
        if mag is None:
            continue
        # USGS ships [lon, lat]; getting this backwards puts California in Asia.
        lon, lat = float(c[0]), float(c[1])
        out.append({
            "lat": lat, "lon": lon, "kind": "earthquake",
            "level": "red" if mag >= 6.5 else "orange" if mag >= 5.5 else "green",
            "title": f"M{mag} — {p.get('place') or 'earthquake'}",
            "place": p.get("place"), "severity": f"M{mag}",
            "mag": mag, "url": p.get("url"), "when": p.get("time"),
            "source": "USGS",
        })
    return out


def fetch_events():
    events, sources, failed = [], [], []
    for name, fn in (("GDACS", _gdacs_events), ("USGS", _usgs_events)):
        try:
            got = fn()
            events += got
            sources.append(f"{name} ({len(got)})")
        except Exception as e:
            print(f"  .. {name}: {e}", file=sys.stderr)
            failed.append(name)

    rank = {"red": 0, "orange": 1, "green": 2}
    events.sort(key=lambda e: (rank.get(e["level"], 3), -(e.get("mag") or 0)))
    counts = {lv: sum(1 for e in events if e["level"] == lv)
              for lv in ("red", "orange", "green")}
    return block(" + ".join(sources) if sources else "no event source reached",
                 ok=bool(events), events=events[:120], event_count=len(events),
                 counts=counts, failed_sources=failed,
                 note="Recorded events with coordinates. Not a threat assessment.")


def main():
    n = now_et()
    print(f"Collecting at {n.isoformat(timespec='seconds')}")
    steps = [
        ("weather", fetch_weather), ("alerts", fetch_alerts), ("air", fetch_air),
        ("commute", fetch_commute), ("closures", fetch_closures),
        ("tropics", fetch_tropics), ("fuel", fetch_fuel),
        ("news", fetch_news), ("fires", fetch_fires),
        ("events", fetch_events),
    ]
    data = {
        "generated_at": stamp(),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "weekday": n.strftime("%A"),
        "date_label": n.strftime("%A, %B %-d"),
        "is_weekend": n.weekday() >= 5,
    }
    failures = []
    for name, fn in steps:
        print(f"-> {name}")
        data[name] = fn()
        if not data[name].get("ok"):
            failures.append(name)

    print("-> world")
    data["world"] = fetch_world(data.get("news") or {})
    if not data["world"].get("ok"):
        failures.append("world")

    data["failures"] = failures
    total = len(steps) + 1
    data["health"] = f"{total - len(failures)}/{total} sources ok"

    os.makedirs("data", exist_ok=True)
    with open("data/brief.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

    print(f"\n{data['health']}")
    if failures:
        print("failed: " + ", ".join(failures))
    # Never exit non-zero for a source outage — a partial brief still ships,
    # and the app marks the gaps. Only a write failure is fatal.
    return 0


if __name__ == "__main__":
    sys.exit(main())
