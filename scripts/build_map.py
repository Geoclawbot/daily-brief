#!/usr/bin/env python3
"""
One-time world map builder.

The app needs country outlines. This downloads Natural Earth's 110m country
set, flattens it to SVG path strings on an equirectangular projection, and
writes data/world.json. It runs in GitHub Actions, which has the network the
authoring sandbox does not.

It is deliberately a separate step from the collector:

  - the geometry never changes, so it runs once and then skips
  - a failure here must not stop the brief from collecting
  - the output is committed, so the app never fetches a third-party map

Run:  python3 scripts/build_map.py [--force]
"""
import json
import os
import sys

SRC = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
       "master/geojson/ne_110m_admin_0_countries.geojson")
OUT = "data/world.json"

# SVG viewbox. Equirectangular: 2:1 is the natural aspect for -180..180 by
# -90..90, so squares stay square and nothing is silently stretched.
W, H = 1000.0, 500.0

# Simplification tolerance in projected units. ~0.35 keeps recognisable
# coastlines while cutting the file to a fraction of the raw geometry.
EPS = 0.35

# Drop islands smaller than this bounding box, in projected units. They are
# invisible at this scale and cost more bytes than the whole of Europe.
MIN_BOX = 1.2


def project(lon, lat):
    """Equirectangular. Returns floats in SVG user units."""
    x = (lon + 180.0) / 360.0 * W
    y = (90.0 - lat) / 180.0 * H
    return x, y


def _perp(p, a, b):
    """Perpendicular distance from p to the segment a-b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def simplify(pts, eps=EPS):
    """Ramer-Douglas-Peucker, iterative so a long coastline cannot blow the
    recursion limit."""
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        worst, at = 0.0, lo
        for i in range(lo + 1, hi):
            d = _perp(pts[i], pts[lo], pts[hi])
            if d > worst:
                worst, at = d, i
        if worst > eps:
            keep[at] = True
            stack.append((lo, at))
            stack.append((at, hi))
    return [p for p, k in zip(pts, keep) if k]


def ring_path(ring):
    """One projected, simplified ring as an SVG subpath, or '' if too small."""
    pts = [project(c[0], c[1]) for c in ring if len(c) >= 2]
    if len(pts) < 4:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(xs) - min(xs)) < MIN_BOX and (max(ys) - min(ys)) < MIN_BOX:
        return ""
    pts = simplify(pts)
    if len(pts) < 3:
        return ""
    out = [f"M{pts[0][0]:.1f} {pts[0][1]:.1f}"]
    for x, y in pts[1:]:
        out.append(f"L{x:.1f} {y:.1f}")
    out.append("Z")
    return "".join(out)


def rings_of(geom):
    """Every outer ring in a Polygon or MultiPolygon. Holes are dropped: at
    this scale a lake reads as noise, not information."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates") or []
    if t == "Polygon":
        return c[:1]
    if t == "MultiPolygon":
        return [poly[0] for poly in c if poly]
    return []


def iso_of(props):
    """Natural Earth writes -99 where a territory has no ISO code; ADM0_A3 is
    always populated, so fall back to it rather than dropping the shape."""
    for key in ("ISO_A3", "ISO_A3_EH", "ADM0_A3"):
        v = (props or {}).get(key)
        if v and v != "-99":
            return str(v).upper()
    return None


def name_of(props):
    for key in ("NAME", "ADMIN", "NAME_LONG", "SOVEREIGNT"):
        v = (props or {}).get(key)
        if v:
            return str(v)
    return None


def build(geojson):
    """GeoJSON dict -> list of {iso, name, d}. Pure, so it is unit tested."""
    out = []
    for f in (geojson or {}).get("features", []):
        props = f.get("properties") or {}
        iso, name = iso_of(props), name_of(props)
        if not iso or not name:
            continue
        d = "".join(ring_path(r) for r in rings_of(f.get("geometry")))
        if d:
            out.append({"iso": iso, "name": name, "d": d})
    out.sort(key=lambda c: c["iso"])
    return out


def main():
    force = "--force" in sys.argv
    if os.path.exists(OUT) and not force:
        print(f"{OUT} already present — nothing to do.")
        return 0

    import requests
    print(f"Downloading {SRC}")
    r = requests.get(SRC, timeout=120)
    r.raise_for_status()
    countries = build(r.json())

    # Loud failure. A map with six countries in it is worse than no map,
    # because it looks like the world is quiet.
    if len(countries) < 150:
        print(f"!! only {len(countries)} countries parsed — refusing to write",
              file=sys.stderr)
        return 1

    os.makedirs("data", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": "Natural Earth 110m admin-0",
                   "projection": "equirectangular",
                   "width": W, "height": H,
                   "countries": countries}, fh, separators=(",", ":"))
    kb = os.path.getsize(OUT) / 1024
    print(f"Wrote {OUT}: {len(countries)} countries, {kb:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
