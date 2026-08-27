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
    out.sort(key=lambda i: ((i.get("magnitude") or 0) == 4, i.get("delay_s") or 0),
             reverse=True)
    return out


def fetch_commute():
    if not TOMTOM_KEY:
        return fail("TomTom", "TOMTOM_API_KEY not set — add it as a repository secret")
    try:
        incidents = _area_incidents()
        majors = [i for i in incidents if (i.get("magnitude") or 0) >= 3]
        area = dict(incidents=incidents[:25], incident_count=len(incidents),
                    major_count=len(majors), coverage="Miami-Dade + Broward")

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

        # His rule: only surface an alternate that beats the default by >10 min.
        worth = [a for a in alts if a["saves"] > 10]

        return block("TomTom Routing + Traffic", route_shown=True,
                     leg=leg["label"], direction=leg["key"], depart_clock=leg["clock"],
                     depart_at=depart, miles=miles, minutes=mins,
                     delay_minutes=delay, toll_free=True, alternates=alts,
                     alternates_worth_taking=worth, **area)
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
                 ok=ok, streams=STREAMS, **out)


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
def main():
    n = now_et()
    print(f"Collecting at {n.isoformat(timespec='seconds')}")
    steps = [
        ("weather", fetch_weather), ("alerts", fetch_alerts), ("air", fetch_air),
        ("commute", fetch_commute), ("closures", fetch_closures),
        ("tropics", fetch_tropics), ("fuel", fetch_fuel),
        ("news", fetch_news), ("fires", fetch_fires),
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

    data["failures"] = failures
    data["health"] = f"{len(steps) - len(failures)}/{len(steps)} sources ok"

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
