#!/usr/bin/env python3
"""
ens_grams.py - build one self-contained HTML page of ECMWF charts:

  Meteograms        10-day and 15-day ENS meteograms for each station, plus
                    the 15-day meteogram from the previous few runs.
  Extended Outlook  Weekly mean anomaly maps (2 m temperature, precipitation
                    and MSLP over Australasia; 500 hPa height over the South
                    Pole) for every available week, and the SH geopotential
                    Hovmoller diagrams (1000 and 500 hPa) for the last few runs.
  Stratosphere      Southern Hemisphere zonal-mean zonal wind (10 hPa).

Usage:
    python ens_grams.py                          # latest runs
    python ens_grams.py --history 6              # compare 6 meteogram runs
    python ens_grams.py --base-time 2026092212   # meteograms from a specific run (UTC)
    python ens_grams.py --no-cache               # re-download everything
    python ens_grams.py --out my_page.html --no-open

Requests are spaced out and retried if ECMWF says "too many requests".
Downloaded charts are kept in an ens_grams_cache folder next to this script
(older than 7 days are deleted), so each run only downloads new charts.

Only the Python standard library is needed. Images are embedded in the HTML,
so the file works offline and can be emailed or archived.

Charts (c) ECMWF, licensed CC BY 4.0 - https://charts.ecmwf.int
"""

import argparse
import base64
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Settings - edit freely
# --------------------------------------------------------------------------
STATIONS = [
    # name shown on the chart, lat, lon
    ("Adelaide",    -34.9257, 138.5832),   # West Terrace
    ("Melbourne",   -37.8255, 144.9816),   # Olympic Park
    ("Bankstown",   -33.9167, 151.0330),
    ("Archerfield", -27.5717, 153.0078),
]

METEOGRAMS = [
    # (epsgram value, label)
    ("classical_10d", "10-day ENS meteogram"),
    ("classical_15d", "15-day ENS meteogram"),
]
HISTORY_PRODUCT = "classical_15d"  # meteogram used for the run-to-run comparison
DEFAULT_HISTORY = 4                # meteogram runs to compare, incl. the latest (12 h apart)

# Weekly mean anomaly maps: (product, projection, label)
ANOMALY_MAPS = [
    ("extended-anomaly-2t",   "opencharts_australasia", "2 m temperature"),
    ("extended-anomaly-tp",   "opencharts_australasia", "Precipitation"),
    ("extended-anomaly-mslp", "opencharts_australasia", "MSLP"),
    ("extended-anomaly-z500", "opencharts_south_pole",  "500 hPa height (South Pole)"),
]

ANOMALY_WEEKS = 5            # how many weeks ahead to show
ANOMALY_HISTORY = 3          # anomaly-map runs to compare, incl. latest (24 h apart)

HOVMOLLER_PRODUCT = "mofc_multi_eps_family_hovmoller"
HOVMOLLERS = [
    # (parameters, label)
    ({"area": "South Hemisphere", "parameter": "geopotential 1000 hPa"}, "SH Hovmöller, 1000 hPa geopotential"),
    ({"area": "South Hemisphere", "parameter": "geopotential 500 hPa"},  "SH Hovmöller, 500 hPa geopotential"),
]
HOVMOLLER_HISTORY = 2        # latest + 1 previous
EXTENDED_STEP_HOURS = 24     # extended-range runs are daily at 00 UTC

STRAT_CHARTS = [
    # (product, parameters, label)
    ("extended-zonal-mean-zonal-wind", {"area": "sh"}, "Zonal-mean zonal wind, 10 hPa (SH)"),
]

# --- Charts from other sites ----------------------------------------------
# Each set gets one run slider. "history" = how many versions to show (latest + previous).
#   kind "static": the site always overwrites the same file, so previous versions are
#                  kept by this script in ens_grams_cache/archive (history builds up over time).
#   kind "dated":  the file name contains the date ({date} = YYYYMMDD); the script looks
#                  back up to "lookback" days for the newest versions.
#   crop: [x0, y0, x1, y1] as fractions of the image to keep (cropped in the browser).
CPC = "https://www.cpc.ncep.noaa.gov/products"
TELE_SETS = [
    {"id": "sam", "title": "Southern Annular Mode", "history": 5, "items": [
        {"label": "CPC AAO index \u2013 observed & GEFS forecast", "kind": "static",
         "url": CPC + "/precip/CWlink/daily_ao_index/aao/aao.gefs.sprd2.png", "crop": [0, 0, 1, 0.29]},
        {"label": "BoM SAM daily index outlook", "kind": "dated", "lookback": 35,
         "url": "https://www.bom.gov.au/clim_data/IDCK000080/sam_index.daily.{date}.png",
         "page": "https://www.bom.gov.au/climate/ocean/outlooks/?index=samL#tabs=Graphs"},
    ]},
    {"id": "mjo", "title": "MJO", "history": 2, "items": [
        {"label": "EMON_BC (ECMWF, bias-corrected)", "kind": "static", "url": CPC + "/precip/mjo/img/EMON_BC.png"},
        {"label": "GMON (NCEP GEFS)", "kind": "static", "url": CPC + "/precip/mjo/img/GMON.png"},
        {"label": "BOMM_BC (BoM, bias-corrected)", "kind": "static", "url": CPC + "/precip/mjo/img/BOMM_BC.png"},
    ]},
]
_SSW = CPC + "/stratosphere/SSW/gif_files/"
STRAT_SETS = [
    {"id": "cpcstrat", "title": "CPC polar-cap temperature and 60\u00b0S zonal wind", "history": 3, "items": [
        {"label": "Temperature 90\u201360\u00b0S, 10 hPa", "kind": "static", "url": _SSW + "T_10mb_9060S.png"},
        {"label": "Temperature 90\u201360\u00b0S, 50 hPa", "kind": "static", "url": _SSW + "T_50mb_9060S.png"},
        {"label": "Temperature 90\u201360\u00b0S, 100 hPa", "kind": "static", "url": _SSW + "T_100mb_9060S.png"},
        {"label": "Zonal wind 60\u00b0S, 10 hPa", "kind": "static", "url": _SSW + "U_10mb_60S.png"},
        {"label": "Zonal wind 60\u00b0S, 50 hPa", "kind": "static", "url": _SSW + "U_50mb_60S.png"},
        {"label": "Zonal wind 60\u00b0S, 100 hPa", "kind": "static", "url": _SSW + "U_100mb_60S.png"},
    ]},
    {"id": "stratobs", "title": "StratObserve anomaly time series (SH, 60\u00b0 zonal-mean zonal wind)", "history": 2, "items": [
        {"label": "GFS", "kind": "dated", "lookback": 10,
         "url": "https://stratobserve.com/plots/{date}/tseries/anoms/gfs_sh-u60_{date}.png",
         "page": "https://stratobserve.com/anom_ts_diags"},
        {"label": "GEOS", "kind": "dated", "lookback": 10,
         "url": "https://stratobserve.com/plots/{date}/tseries/anoms/geos_sh-u60_{date}.png",
         "page": "https://stratobserve.com/anom_ts_diags"},
    ]},
]

API = "https://charts.ecmwf.int/opencharts-api/v1/products/{product}/"
SITE = "https://charts.ecmwf.int/products/{product}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ens-grams-script/1.7; personal chart viewer)"}
TIMEOUT = 60
REQUEST_GAP = 1.5      # seconds between requests to ECMWF (raise if you still see 429s)
MAX_RETRIES = 4        # retries per request after "too many requests" / busy server
CACHE_DIR = Path(__file__).resolve().parent / "ens_grams_cache"  # saved charts; old runs never change
LOCAL_TZ = timezone(timedelta(hours=10), "AEST")  # page timestamps shown in AEST


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
_last_request = [0.0]
_lock = threading.Lock()


def http_get(url):
    body, headers = http_get_full(url)
    return body, headers.get("Content-Type", "")


def http_get_full(url):
    """GET with polite pacing, and retries with back-off on 429 / busy server."""
    for attempt in range(MAX_RETRIES + 1):
        with _lock:  # space requests out, even across threads
            wait = _last_request[0] + REQUEST_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == MAX_RETRIES:
                raise
            try:
                delay = float(e.headers.get("Retry-After", ""))
            except ValueError:
                delay = 5 * 2 ** attempt           # 5, 10, 20, 40 s
            delay = min(delay, 90)
            print(f"    server said {e.code}; waiting {delay:.0f}s and retrying...")
            time.sleep(delay)


def _fmt(params, iso):
    out = {}
    for k, v in params.items():
        if isinstance(v, datetime):
            v = v.strftime("%Y-%m-%dT%H:%M:%SZ") if iso else v.strftime("%Y%m%d%H%M")
        out[k] = v
    return out


def api_url(product, params):
    return API.format(product=product) + "?" + urllib.parse.urlencode(_fmt(params, True))


def site_url(product, params):
    return SITE.format(product=product) + "?" + urllib.parse.urlencode(_fmt(params, False))


def image_link(product, params):
    """Ask the OpenCharts API for the image link of one chart."""
    body, _ = http_get(api_url(product, params))
    data = json.loads(body)
    try:
        return data["data"]["link"]["href"]
    except (KeyError, TypeError):
        raise RuntimeError(f"Unexpected API response: {str(data)[:300]}")


def exists(product, params):
    """True if the chart exists (or is already saved)."""
    if cache_path(product, params).exists():
        return True
    try:
        image_link(product, params)
        return True
    except Exception:
        return False


def cache_path(product, params):
    key = json.dumps(_fmt(params, True), sort_keys=True)
    h = hashlib.sha1(key.encode()).hexdigest()[:12]
    bt = params.get("base_time")
    stamp = bt.strftime("%Y%m%d%H") if isinstance(bt, datetime) else "x"
    return CACHE_DIR / f"{stamp}_{product}_{h}.img"


USE_CACHE = [True]


def fetch(product, params):
    """Returns (data_uri, error, from_cache)."""
    cp = cache_path(product, params)
    meta = cp.with_suffix(".type")
    try:
        if USE_CACHE[0] and cp.exists() and cp.stat().st_size > 0:
            img = cp.read_bytes()
            ctype = meta.read_text().strip() if meta.exists() else "image/png"
            cached = True
        else:
            img, ctype = http_get(image_link(product, params))
            ctype = ctype.split(";")[0].strip() or "image/png"
            cached = False
            try:
                CACHE_DIR.mkdir(exist_ok=True)
                cp.write_bytes(img)
                meta.write_text(ctype)
            except OSError:
                pass
        return "data:%s;base64,%s" % (ctype, base64.b64encode(img).decode()), None, cached
    except Exception as e:  # keep going; show the error on the page
        return None, f"{type(e).__name__}: {e}", False


def split_sets(sets, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for sid, st in sets.items():
        for i, it in enumerate(st["items"]):
            for k, v in enumerate(it["v"]):
                if v["s"] and v["s"].startswith("data:"):
                    head, b64 = v["s"].split(",", 1)
                    img = base64.b64decode(b64)
                    name = "x_" + hashlib.sha1(img).hexdigest()[:16] + {"image/gif": ".gif", "image/jpeg": ".jpg"}.get(head[5:].split(";")[0], ".png")
                    (folder / name).write_bytes(img)
                    v["s"] = f"{folder.name}/{name}"


def split_images(R, folder):
    """Write embedded images out as files; the page then links to charts/<name>."""
    folder.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}
    out = {}
    for key, (src, err, cached) in R.items():
        if src and src.startswith("data:"):
            head, b64 = src.split(",", 1)
            ctype = head[5:].split(";")[0]
            name = hashlib.sha1(repr(key).encode()).hexdigest()[:16] + ext.get(ctype, ".png")
            (folder / name).write_bytes(base64.b64decode(b64))
            src = f"{folder.name}/{name}"
        out[key] = (src, err, cached)
    return out


# --------------------------------------------------------------------------
# Charts from other sites (CPC, BoM, StratObserve)
# --------------------------------------------------------------------------
ARCHIVE_DIR = CACHE_DIR / "archive"
MISSING_FILE = CACHE_DIR / "missing.json"


def _data_uri(img, ctype):
    ctype = (ctype or "image/png").split(";")[0].strip() or "image/png"
    return "data:%s;base64,%s" % (ctype, base64.b64encode(img).decode())


def _sniff(img):
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img[:3] == b"GIF":
        return "image/gif"
    if img[:2] == b"\xff\xd8":
        return "image/jpeg"
    return None


def fetch_static_versions(item, n):
    """Current image plus previous versions this script has archived. Newest first."""
    key = hashlib.sha1(item["url"].encode()).hexdigest()[:12]
    index_path = ARCHIVE_DIR / f"{key}.json"
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError):
        index = []
    err = None
    try:
        img, headers = http_get_full(item["url"])
        if not _sniff(img):
            raise RuntimeError("the site did not return an image")
        h = hashlib.sha1(img).hexdigest()[:16]
        if not index or index[-1]["hash"] != h:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            fname = f"{key}_{h}.img"
            (ARCHIVE_DIR / fname).write_bytes(img)
            lm = headers.get("Last-Modified")
            try:
                when = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc) if lm else None
            except ValueError:
                when = None
            index.append({"hash": h, "file": fname, "type": _sniff(img),
                          "time": (when or datetime.now(timezone.utc)).isoformat(), "from_header": bool(when)})
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    # keep only what is needed
    keep = index[-max(n, 1):]
    for old in index[:-max(n, 1)]:
        try:
            (ARCHIVE_DIR / old["file"]).unlink()
        except OSError:
            pass
    if keep != index or err is None:
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            index_path.write_text(json.dumps(keep))
        except OSError:
            pass
    versions = []
    for ent in reversed(keep):
        t = datetime.fromisoformat(ent["time"])
        lab = ("updated " if ent.get("from_header") else "fetched ") + f"{t:%a %d %b %H:%M} UTC"
        try:
            src = _data_uri((ARCHIVE_DIR / ent["file"]).read_bytes(), ent.get("type"))
            versions.append({"s": src, "e": None, "lab": lab, "l": item.get("page", item["url"])})
        except OSError as e:
            versions.append({"s": None, "e": str(e), "lab": lab, "l": item.get("page", item["url"])})
    if err and versions:
        versions[0]["lab"] += " (latest download failed - showing last saved)"
    if not versions:
        versions.append({"s": None, "e": err or "no image", "lab": "", "l": item.get("page", item["url"])})
    return versions, err


def fetch_dated_versions(item, n):
    """Newest n dated images found within the look-back window. Newest first."""
    try:
        missing = set(json.loads(MISSING_FILE.read_text()))
    except (OSError, ValueError):
        missing = set()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    versions, last_err = [], None
    for back in range(-1, item.get("lookback", 14) + 1):
        if len(versions) >= n:
            break
        d = today - timedelta(days=back)
        url = item["url"].format(date=d.strftime("%Y%m%d"))
        if url in missing:
            continue
        cp = CACHE_DIR / ("url_" + hashlib.sha1(url.encode()).hexdigest()[:16] + ".img")
        try:
            if USE_CACHE[0] and cp.exists() and cp.stat().st_size > 0:
                img = cp.read_bytes()
            else:
                img, _ = http_get(url)
                if not _sniff(img):
                    raise RuntimeError("not an image")
                try:
                    CACHE_DIR.mkdir(exist_ok=True)
                    cp.write_bytes(img)
                except OSError:
                    pass
            versions.append({"s": _data_uri(img, _sniff(img)), "e": None,
                             "lab": f"issued {d:%a %d %b}", "l": item.get("page", url)})
        except urllib.error.HTTPError as e:
            last_err = f"HTTPError {e.code}"
            if e.code == 404 and back > 3:      # old dates never appear later
                missing.add(url)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        MISSING_FILE.write_text(json.dumps(sorted(missing)))
    except OSError:
        pass
    if not versions:
        versions.append({"s": None, "e": last_err or "nothing found", "lab": "", "l": item.get("page", item["url"])})
        return versions, last_err
    return versions, None


def collect_sets(sets):
    out = {}
    for st in sets:
        items = []
        for it in st["items"]:
            fn = fetch_static_versions if it["kind"] == "static" else fetch_dated_versions
            versions, err = fn(it, st["history"])
            got = sum(1 for v in versions if v["s"])
            print(f"  {st['id']:<9} {it['label'][:44]:<46} {got} version(s)" + (f"  [{err}]" if err else ""))
            items.append({"label": it["label"], "crop": it.get("crop"), "v": versions})
        out[st["id"]] = {"title": st["title"], "n": st["history"], "items": items}
    return out


def prune_cache(days=7):
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - days * 86400
    for f in CACHE_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def recent_runs(step_hours, n):
    """Most recent run times on a `step_hours` cycle (00 UTC based), newest first."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t = now - timedelta(hours=now.hour % step_hours)
    return [t - timedelta(hours=step_hours * i) for i in range(n)]


def find_latest(label, candidates, test):
    for bt in candidates:
        if test(bt):
            print(f"  {label}: {bt:%Y-%m-%d %H} UTC")
            return bt
    print(f"  {label}: no run found in the last few days")
    return None


def mondays_after(bt, days=46):
    """Candidate weekly valid times: Mondays 00 UTC within the extended range."""
    d = (bt + timedelta(days=1)).replace(hour=0)
    d += timedelta(days=(7 - d.weekday()) % 7)
    out = []
    while d <= bt + timedelta(days=days):
        out.append(d)
        d += timedelta(days=7)
    return out


def run_label(bt, short=False):
    if short:
        return f"{bt:%d %b %H}Z"
    return f"{bt:%a %d %b %H} UTC ({bt.astimezone(LOCAL_TZ):%a %H:%M} AEST)"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --muted:#5b6470; --line:#dde1e6; --accent:#1f5fa8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15181c; --card:#1e2227; --ink:#e6e9ed; --muted:#9aa3ad; --line:#2d333a; --accent:#6ea8ec; }
}
* { box-sizing:border-box; }
body { margin:0; font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--ink); }
header { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); padding:10px 16px 0; }
.top { display:flex; flex-wrap:wrap; gap:4px 20px; align-items:baseline; }
header h1 { font-size:17px; margin:0; }
header .meta { color:var(--muted); font-size:13px; }
.tabs { display:flex; gap:2px; margin-top:8px; flex-wrap:wrap; }
.tabs button { font:inherit; font-size:14px; padding:7px 14px; border:0; border-bottom:3px solid transparent;
               background:transparent; color:var(--muted); cursor:pointer; }
.tabs button.on { color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
.bar { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:0 0 14px; }
.bar .sep { width:1px; height:18px; background:var(--line); margin:0 4px; }
.bar a, .bar button, .ctl button { font:inherit; font-size:13px; padding:4px 10px; border:1px solid var(--line); border-radius:999px;
        background:transparent; color:var(--ink); text-decoration:none; cursor:pointer; }
.bar button.on, .ctl button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
main { padding:16px; max-width:1800px; margin:0 auto; }
.tab { display:none; }
body[data-tab=met] #tab-met, body[data-tab=ext] #tab-ext, body[data-tab=tele] #tab-tele, body[data-tab=strat] #tab-strat { display:block; }
.rs { --col:440px; --sbs:400px; }
.rs.size-s { --col:320px; --sbs:300px; }
.rs.size-l { --col:640px; --sbs:560px; }
.rs .rs-flick { grid-template-columns:repeat(auto-fill, min(var(--col), 100%)); }
.rs .rs-sbs { display:none; }
.rs.sbs .rs-sbs { display:block; }
.rs.sbs .rs-flick { display:none; }
.rs .rs-sbs .row { grid-template-columns:repeat(auto-fill, min(var(--sbs), 100%)); gap:12px; margin-bottom:14px; }
.rs .when { display:block; font-size:12px; }
#tab-strat > section:first-child .row.two { grid-template-columns:repeat(auto-fill, min(620px, 100%)); }
section { margin-bottom:28px; scroll-margin-top:110px; }
h2 { font-size:18px; margin:0 0 4px; }
h3 { font-size:16px; margin:0 0 8px; }
h3 small, h2 small { font-weight:400; color:var(--muted); font-size:13px; }
.row { display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:12px; }
.row.two { grid-template-columns:repeat(auto-fit,minmax(460px,1fr)); }
figure { margin:0; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:6px; }
figcaption { font-size:13px; color:var(--muted); margin-bottom:4px; display:flex; justify-content:space-between; gap:8px; }
figcaption a { color:var(--accent); white-space:nowrap; }
figure img { width:100%; height:auto; display:block; background:#fff; border-radius:4px; cursor:zoom-in; }
.err { padding:40px 12px; text-align:center; color:#b3261e; font-size:13px; }
.hint { color:var(--muted); font-size:13px; margin:0 0 12px; }
/* meteogram view modes */
.cmp { display:none; }
#tab-met.only-10d .p-classical_10d, #tab-met.only-15d .p-classical_15d { grid-column:1/-1; }
#tab-met.only-10d .p-classical_15d, #tab-met.only-15d .p-classical_10d { display:none; }
#tab-met.compare .latest { display:none; }
#tab-met.compare .cmp, #tab-ext .cmp { display:block; }
#tab-met:not(.compare) .hint.cmp-hint { display:none; }
/* run-comparison viewers */
.ctl { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; margin-bottom:8px; font-size:13px; }
.ctl input[type=range] { flex:1; min-width:140px; accent-color:var(--accent); }
.ctl .lab { font-weight:600; min-width:9em; }
.flick figure { max-width:1100px; }
.cmp .grid { display:none; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:10px; }
.cmp.sbs .grid { display:grid; }
.cmp.sbs .flick { display:none; }
/* weekly anomalies */
.wk-all { display:none; }
#anoms.all .wk-one { display:none; }
#anoms.all .wk-all { display:block; }
.wk { display:none; }
.wk.on { display:block; }
/* chart sizes: fixed column widths per S / M / L, so panels never stretch to fill the screen */
#tab-met { --col:520px; --flick:760px; }
#tab-met.size-s { --col:380px; --flick:560px; }
#tab-met.size-l { --col:760px; --flick:1100px; }
#anoms { --col:460px; --strip:240px; --sbs:420px; }
#anoms.size-s { --col:320px; --strip:180px; --sbs:300px; }
#anoms.size-l { --col:680px; --strip:340px; --sbs:600px; }
#anoms .an-sbs { display:none; }
#anoms.sbs .an-sbs { display:block; }
#anoms.sbs .an-flick { display:none; }
#anoms .an-sbs .row { grid-template-columns:repeat(auto-fill, min(var(--sbs), 100%)); gap:12px; margin-bottom:14px; }
#an-ctl { margin-top:4px; }
#hov { --col:560px; }
#hov.size-s { --col:400px; }
#hov.size-l { --col:820px; }
#tab-met .row, #anoms .row.two, #hov .row.two { grid-template-columns:repeat(auto-fill, min(var(--col), 100%)); }
#tab-met .cmp .grid { grid-template-columns:repeat(auto-fill, min(var(--col), 100%)); }
#tab-met .flick figure { max-width:var(--flick); }
#anoms .strip { grid-template-columns:repeat(auto-fill,minmax(min(var(--strip),100%),1fr)); }
#hov .flick figure { max-width:none; }
#tab-met.only-10d .p-classical_10d, #tab-met.only-15d .p-classical_15d { grid-column:auto; }
/* notes */
.notes { margin:12px 0 4px; }
.notes label { display:flex; justify-content:space-between; font-size:13px; font-weight:600; margin-bottom:4px; }
.notes label .saved { font-weight:400; color:var(--muted); }
.notes textarea { width:100%; min-height:90px; resize:vertical; font:inherit; font-size:14px; line-height:1.45;
  padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink); }
.notes textarea:focus { outline:2px solid var(--accent); outline-offset:-1px; }
.savebtn { margin-left:auto; font:inherit; font-size:13px; padding:4px 10px; border:1px solid var(--line); border-radius:999px;
  background:transparent; color:var(--ink); cursor:pointer; }
.wk-all .strip { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:8px; margin-bottom:18px; }
#zoom { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none; align-items:center; justify-content:center; z-index:10; cursor:zoom-out; }
#zoom img { max-width:98vw; max-height:98vh; background:#fff; }
footer { color:var(--muted); font-size:12px; padding:0 16px 24px; text-align:center; }
@media (max-width:520px) { .row, .row.two, .cmp .grid { grid-template-columns:1fr; } }
@media (max-width:600px) {
  header { padding:6px 12px 0; }
  header h1 { font-size:15px; }
  header .meta { font-size:12px; }
  .savebtn { padding:3px 8px; font-size:12px; }
  .tabs { margin-top:4px; flex-wrap:nowrap; overflow-x:auto; }
  .tabs button { padding:6px 10px; font-size:13px; white-space:nowrap; }
  main { padding:12px; }
}
"""

JS = r"""
const body = document.body, met = document.getElementById('tab-met');
function setTab(t) {
  body.dataset.tab = t;
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  try { localStorage.setItem('ensTab', t); } catch (e) {}
}
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => setTab(b.dataset.tab));
try { const t = localStorage.getItem('ensTab'); if (t && document.getElementById('tab-' + t)) setTab(t); } catch (e) {}

document.querySelectorAll('#met-modes button').forEach(b => b.onclick = () => {
  met.classList.remove('only-10d', 'only-15d', 'compare');
  if (b.dataset.mode !== 'both') met.classList.add(b.dataset.mode);
  document.querySelectorAll('#met-modes button').forEach(x => x.classList.toggle('on', x === b));
});

// run-comparison viewers; viewers sharing data-group move together
const groups = {};
document.querySelectorAll('.cmp[data-runs]').forEach(v => {
  const g = v.dataset.group;
  (groups[g] = groups[g] || {viewers: [], idx: 0, n: 0, timer: null}).viewers.push(v);
  v._runs = JSON.parse(v.dataset.runs);
  groups[g].n = v._runs.length; groups[g].idx = v._runs.length - 1;
});
function show(g, i) {
  const G = groups[g]; if (!G || !G.n) return;
  G.idx = (i + G.n) % G.n;
  G.viewers.forEach(v => {
    const r = v._runs[G.idx];
    const img = v.querySelector('.flick img'), err = v.querySelector('.flick .err');
    if (r.src) { img.src = r.src; img.style.display = ''; err.style.display = 'none'; }
    else { img.style.display = 'none'; err.style.display = ''; err.textContent = 'Not available: ' + (r.err || ''); }
    v.querySelector('.flick .lab').textContent = r.label + (G.idx === G.n - 1 ? '  (latest)' : '');
    v.querySelector('.flick figcaption a').href = r.link;
    v.querySelector('input[type=range]').value = G.idx;
  });
}
Object.entries(groups).forEach(([g, G]) => {
  G.viewers.forEach(v => {
    v.querySelector('input[type=range]').oninput = e => show(g, +e.target.value);
    v.querySelector('.prev').onclick = () => show(g, G.idx - 1);
    v.querySelector('.next').onclick = () => show(g, G.idx + 1);
    v.querySelector('.play').onclick = () => {
      if (G.timer) { clearInterval(G.timer); G.timer = null; }
      else G.timer = setInterval(() => show(g, G.idx + 1), 1200);
      G.viewers.forEach(w => w.querySelector('.play').classList.toggle('on', !!G.timer));
    };
    v.querySelectorAll('.layout button').forEach(b => b.onclick = () => G.viewers.forEach(w => {
      w.classList.toggle('sbs', b.dataset.lay === 'sbs');
      w.querySelectorAll('.layout button').forEach(x => x.classList.toggle('on', x.dataset.lay === b.dataset.lay));
    }));
  });
  show(g, G.n - 1);
});
// weekly anomaly maps: one run selector for every map
const AN = (() => {
  const el = document.getElementById('anom-data'); if (!el) return null;
  const data = JSON.parse(el.textContent), n = data.runs.length;
  const ctl = document.getElementById('an-ctl'), sec = document.getElementById('anoms');
  const st = { idx: n - 1, n, timer: null };
  function fill(fig, ri) {
    const c = data.cells[fig.dataset.v + '|' + fig.dataset.w][ri];
    const img = fig.querySelector('img'), err = fig.querySelector('.err');
    if (c && c.s) {
      if (img.dataset.orig !== c.s) { img.dataset.orig = c.s; img.src = c.s; }
      img.style.display = ''; err.style.display = 'none';
    } else {
      img.style.display = 'none'; err.style.display = '';
      err.textContent = 'Not available' + (c && c.e ? ': ' + c.e : '');
    }
    if (c) fig.querySelector('a').href = c.l;
  }
  function show(i) {
    st.idx = (i + n) % n;
    sec.querySelectorAll('.anf:not([data-r])').forEach(f => fill(f, st.idx));
    ctl.querySelector('.lab').textContent = 'Run ' + data.runs[st.idx] + (st.idx === n - 1 ? '  (latest)' : '');
    ctl.querySelector('input').value = st.idx;
  }
  sec.querySelectorAll('.anf[data-r]').forEach(f => fill(f, +f.dataset.r));
  ctl.querySelector('input').oninput = e => show(+e.target.value);
  ctl.querySelector('.prev').onclick = () => show(st.idx - 1);
  ctl.querySelector('.next').onclick = () => show(st.idx + 1);
  ctl.querySelector('.play').onclick = () => {
    if (st.timer) { clearInterval(st.timer); st.timer = null; } else st.timer = setInterval(() => show(st.idx + 1), 1200);
    ctl.querySelector('.play').classList.toggle('on', !!st.timer);
  };
  ctl.querySelectorAll('.layout button').forEach(b => b.onclick = () => {
    sec.classList.toggle('sbs', b.dataset.lay === 'sbs');
    ctl.querySelectorAll('.layout button').forEach(x => x.classList.toggle('on', x === b));
  });
  show(n - 1);
  return { show, st };
})();

// run sets (Teleconnections / Stratosphere): one slider per set
const RS = [...document.querySelectorAll('.rs')].map(sec => {
  const data = JSON.parse(sec.querySelector('.rs-data').textContent), n = data.n;
  const ctl = sec.querySelector('.rs-ctl');
  const st = { sec, idx: n - 1, n, timer: null };       // slider: 0 = oldest, n-1 = latest
  function fill(fig, k) {                                // k: 0 = latest, 1 = previous, ...
    const v = data.items[+fig.dataset.i].v[k];
    const img = fig.querySelector('img'), err = fig.querySelector('.err');
    if (v && v.s) {
      if (img.dataset.orig !== v.s) { img.dataset.orig = v.s; img.src = v.s; }
      img.style.display = ''; err.style.display = 'none';
    } else {
      img.style.display = 'none'; err.style.display = '';
      err.textContent = v ? 'Not available: ' + (v.e || '') : 'No earlier version saved yet';
    }
    fig.querySelector('.when').textContent = v ? (v.lab || '') : '';
    if (v) fig.querySelector('a').href = v.l;
  }
  function show(i) {
    st.idx = (i + n) % n;
    const k = n - 1 - st.idx;
    sec.querySelectorAll('.rs-flick .rsf').forEach(f => fill(f, k));
    if (ctl) {
      ctl.querySelector('.lab').textContent = k === 0 ? 'Latest' : k === 1 ? 'Previous' : k + ' versions back';
      ctl.querySelector('input').value = st.idx;
    }
  }
  sec.querySelectorAll('.rs-sbs .rsf').forEach(f => fill(f, +f.dataset.k));
  if (ctl) {
    ctl.querySelector('input').oninput = e => show(+e.target.value);
    ctl.querySelector('.prev').onclick = () => show(st.idx - 1);
    ctl.querySelector('.next').onclick = () => show(st.idx + 1);
    ctl.querySelector('.play').onclick = () => {
      if (st.timer) { clearInterval(st.timer); st.timer = null; } else st.timer = setInterval(() => show(st.idx + 1), 1200);
      ctl.querySelector('.play').classList.toggle('on', !!st.timer);
    };
    ctl.querySelectorAll('.layout button').forEach(b => b.onclick = () => {
      sec.classList.toggle('sbs', b.dataset.lay === 'sbs');
      ctl.querySelectorAll('.layout button').forEach(x => x.classList.toggle('on', x === b));
    });
  }
  show(n - 1);
  st.show = show;
  return st;
});
function activeRunset() {      // the set nearest the top of the screen in the current tab
  const tab = document.getElementById('tab-' + body.dataset.tab);
  let best = null;
  RS.forEach(r => {
    if (!tab || !tab.contains(r.sec) || r.n < 2) return;
    const t = r.sec.getBoundingClientRect().top;
    if (t < innerHeight / 2 && (!best || t > best.t)) best = { r, t };
  });
  return best ? best.r : null;
}

function activeGroup() {
  if (body.dataset.tab === 'met' && met.classList.contains('compare')) return 'met';
  if (body.dataset.tab === 'ext') {
    const hov = document.getElementById('hov');
    if (!AN || (hov && hov.getBoundingClientRect().top < innerHeight / 2)) return 'hov';
    return 'anom';
  }
  if (body.dataset.tab === 'tele' || body.dataset.tab === 'strat') return 'rs';
  return null;
}

// weekly anomaly week picker
const anoms = document.getElementById('anoms');
if (anoms) {
  const btns = [...anoms.querySelectorAll('.wkbtn')], weeks = [...anoms.querySelectorAll('.wk')];
  function pickWeek(i) {
    anoms.classList.toggle('all', i === 'all');
    btns.forEach(b => b.classList.toggle('on', b.dataset.wk === String(i)));
    weeks.forEach((w, j) => w.classList.toggle('on', j === i));
  }
  btns.forEach(b => b.onclick = () => pickWeek(b.dataset.wk === 'all' ? 'all' : +b.dataset.wk));
  if (weeks.length) pickWeek(0);
}

// S / M / L size pickers (each remembered)
document.querySelectorAll('.sizer').forEach(sp => {
  const target = document.getElementById(sp.dataset.target); if (!target) return;
  const set = sz => {
    target.classList.remove('size-s', 'size-l');
    if (sz !== 'm') target.classList.add('size-' + sz);
    sp.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.size === sz));
    try { localStorage.setItem('ensSize:' + sp.dataset.target, sz); } catch (e) {}
  };
  sp.querySelectorAll('button').forEach(b => b.onclick = () => set(b.dataset.size));
  try { const sz = localStorage.getItem('ensSize:' + sp.dataset.target); if (sz) set(sz); } catch (e) {}
});

// trim white margins off meteograms (done in the browser, once per image)
const TRIM_SELECTOR = '#tab-met figure img, #anoms figure img, #hov figure img, #tab-tele figure img, #tab-strat figure img';
const trimmed = new Map();              // original src -> trimmed src
const trimmedSet = new Set();
function trim(img) {
  const src = img.currentSrc || img.src;
  if (!src || trimmedSet.has(src)) return;
  if (trimmed.has(src)) { img.src = trimmed.get(src); return; }
  const fig = img.closest('figure'), cr = fig && fig.dataset.crop ? fig.dataset.crop.split(',').map(Number) : null;
  const W = img.naturalWidth, H = img.naturalHeight;
  if (!W || !H) return;
  const sx = cr ? Math.round(cr[0] * W) : 0, sy = cr ? Math.round(cr[1] * H) : 0;
  const w = cr ? Math.round((cr[2] - cr[0]) * W) : W, h = cr ? Math.round((cr[3] - cr[1]) * H) : H;
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  const cx = c.getContext('2d'); cx.drawImage(img, sx, sy, w, h, 0, 0, w, h);
  let d; try { d = cx.getImageData(0, 0, w, h).data; } catch (e) { return; }
  const blank = (x, y) => { const i = (y * w + x) * 4; return d[i + 3] < 10 || (d[i] > 245 && d[i + 1] > 245 && d[i + 2] > 245); };
  const rowBlank = y => { for (let x = 0; x < w; x += 2) if (!blank(x, y)) return false; return true; };
  const colBlank = (x, y0, y1) => { for (let y = y0; y <= y1; y += 2) if (!blank(x, y)) return false; return true; };
  let top = 0, bot = h - 1, left = 0, right = w - 1;
  while (top < bot && rowBlank(top)) top++;
  while (bot > top && rowBlank(bot)) bot--;
  while (left < right && colBlank(left, top, bot)) left++;
  while (right > left && colBlank(right, top, bot)) right--;
  const pad = 6;
  top = Math.max(0, top - pad); left = Math.max(0, left - pad);
  bot = Math.min(h - 1, bot + pad); right = Math.min(w - 1, right + pad);
  const cw = right - left + 1, ch = bot - top + 1;
  if (!cr && cw * ch > 0.97 * w * h) { trimmedSet.add(src); return; }   // nothing worth trimming
  const o = document.createElement('canvas'); o.width = cw; o.height = ch;
  o.getContext('2d').drawImage(c, left, top, cw, ch, 0, 0, cw, ch);
  const out = o.toDataURL('image/png');
  trimmed.set(src, out); trimmedSet.add(out);
  img.src = out;
}
document.addEventListener('load', e => {
  if (e.target.tagName === 'IMG' && e.target.matches(TRIM_SELECTOR)) trim(e.target);
}, true);
document.querySelectorAll(TRIM_SELECTOR).forEach(img => { if (img.complete) trim(img); });

// notes: auto-saved in this browser; "Save copy with notes" writes them into a downloadable page
document.querySelectorAll('.notes textarea').forEach(t => {
  const key = 'ensnotes:' + t.dataset.key, saved = t.closest('.notes').querySelector('.saved');
  try { const v = localStorage.getItem(key); if (v !== null && v !== '') t.value = v; } catch (e) {}
  let tm;
  t.addEventListener('input', () => {
    clearTimeout(tm);
    tm = setTimeout(() => {
      try { localStorage.setItem(key, t.value); saved.textContent = 'saved'; }
      catch (e) { saved.textContent = 'not saved - use "Save copy with notes"'; }
      setTimeout(() => { if (saved.textContent === 'saved') saved.textContent = ''; }, 1500);
    }, 400);
  });
});
document.getElementById('savecopy').onclick = () => {
  const doc = document.documentElement.cloneNode(true);
  doc.querySelectorAll('img[src]').forEach(i => {        // keep image links working in the copy
    const a = i.getAttribute('src'); if (a && !a.startsWith('data:')) i.setAttribute('src', new URL(a, location.href).href);
  });
  const live = document.querySelectorAll('.notes textarea');
  doc.querySelectorAll('.notes textarea').forEach((t, i) => { t.textContent = live[i].value; });
  doc.querySelectorAll('.saved').forEach(s => s.textContent = '');
  doc.querySelector('#zoom').style.display = 'none';
  const html = '<!doctype html>\n' + doc.outerHTML;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([html], {type: 'text/html'}));
  const name = (location.pathname.split('/').pop() || 'ens_grams.html').replace(/(_notes)?\.html?$/i, '');
  a.download = name + '_notes.html';
  document.body.appendChild(a); a.click(); a.remove();
};

const z = document.getElementById('zoom');
document.addEventListener('click', e => {
  if (e.target.matches('figure img')) { z.firstElementChild.src = e.target.src; z.style.display = 'flex'; }
});
z.onclick = () => z.style.display = 'none';
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { z.style.display = 'none'; return; }
  if (e.target.matches('textarea, input')) return;
  const g = activeGroup(); if (!g) return;
  const step = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0; if (!step) return;
  if (g === 'rs') { const r = activeRunset(); if (r) r.show(r.idx + step); }
  else if (g === 'anom') AN.show(AN.st.idx + step); else show(g, groups[g].idx + step);
});
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def figure_html(label, link, res, alt, cls=""):
    src, err = res[0], res[1]
    body = (f'<img loading="lazy" src="{src}" alt="{esc(alt)}">' if src
            else f'<div class="err">Could not fetch this chart<br>{esc(err or "")}</div>')
    return (f'<figure class="{cls}"><figcaption><span>{esc(label)}</span>'
            f'<a href="{esc(link)}" target="_blank" rel="noopener">open on ECMWF</a></figcaption>'
            f'{body}</figure>')


def sizer(target):
    return (f'<span class="sizer" data-target="{target}" style="display:contents">'
            f'<button data-size="s" title="Small">S</button><button data-size="m" class="on" title="Medium">M</button>'
            f'<button data-size="l" title="Large">L</button></span>')


def notes_html(key, title):
    return (f'<div class="notes"><label><span>{esc(title)}</span><span class="saved"></span></label>'
            f'<textarea data-key="{esc(key)}" placeholder="Your thoughts..."></textarea></div>')


def viewer_html(group, title, runs, latest_bt):
    """runs: list of (bt, link, result), oldest first."""
    data = [{"label": run_label(bt), "src": r[0], "err": r[1], "link": link} for bt, link, r in runs]
    grid = "".join(figure_html(run_label(bt, True) + ("  (latest)" if bt == latest_bt else ""),
                               link, r, f"{title} {run_label(bt, True)}") for bt, link, r in runs)
    return (
        f'<div class="cmp" data-group="{group}" data-runs="{esc(json.dumps(data))}">'
        f'<div class="flick"><div class="ctl">'
        f'<button class="prev" title="Older run">&larr;</button>'
        f'<input type="range" min="0" max="{len(runs) - 1}" step="1">'
        f'<button class="next" title="Newer run">&rarr;</button>'
        f'<button class="play">Play</button><span class="lab"></span>'
        f'<span class="layout" style="margin-left:auto;display:flex;gap:6px">'
        f'<button data-lay="flick" class="on">Flick</button><button data-lay="sbs">Side by side</button></span>'
        f'</div><figure><figcaption><span>{esc(title)}</span>'
        f'<a target="_blank" rel="noopener">open on ECMWF</a></figcaption>'
        f'<img alt="{esc(title)}"><div class="err" style="display:none"></div></figure></div>'
        f'<div class="grid">{"".join(grid)}</div></div>')


def meteogram_tab(met_latest, met_history, R):
    if met_latest is None:
        return '<p class="err">No meteogram run found.</p>'
    sections, nav = [], []
    for name, lat, lon in STATIONS:
        sid = "st-" + name.lower().replace(" ", "-")
        nav.append(f'<a href="#{sid}">{esc(name)}</a>')
        base = {"base_time": met_latest, "lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "station_name": name}
        figs = []
        for eg, label in METEOGRAMS:
            p = dict(base, epsgram=eg)
            figs.append(figure_html(label, site_url("opencharts_meteogram", p),
                                    R[("met", name, eg, met_latest)], f"{name} {label}", f"p-{eg}"))
        runs = []
        for bt in met_history:
            p = dict(base, base_time=bt, epsgram=HISTORY_PRODUCT)
            runs.append((bt, site_url("opencharts_meteogram", p), R[("met", name, HISTORY_PRODUCT, bt)]))
        sections.append(
            f'<section id="{sid}"><h3>{esc(name)} <small>{lat:.2f}, {lon:.2f}</small></h3>'
            f'<div class="row latest">{"".join(figs)}</div>'
            f'{viewer_html("met", "15-day ENS meteogram", runs, met_latest)}</section>')
    return (
        f'<div class="bar">{"".join(nav)}<span class="sep"></span><span id="met-modes" style="display:contents">'
        f'<button data-mode="both" class="on">Both</button><button data-mode="only-10d">10-day</button>'
        f'<button data-mode="only-15d">15-day</button><button data-mode="compare">Compare runs (15-day)</button></span>'
        f'<span class="sep"></span>{sizer("tab-met")}</div>'
        f'<p class="meta hint">Latest run {run_label(met_latest)}</p>'
        f'<p class="hint cmp-hint">Comparing the last {len(met_history)} runs of the 15-day meteogram. Use the slider or '
        f'&larr; / &rarr; keys to step through runs, or switch to side by side. Each run starts 12 h later, '
        f'so the time axis shifts slightly between runs.</p>' + "\n".join(sections))


def extended_tab(anom_bt, weeks, anom_history, hov_latest, hov_history, R):
    parts = []
    # weekly mean anomalies, with run-to-run comparison
    parts.append('<section id="anoms"><h2>Weekly mean anomalies')
    if anom_bt is None or not weeks:
        parts.append('</h2><p class="err">No weekly anomaly charts found.</p></section>')
    else:
        parts.append(f' <small>latest run {run_label(anom_bt)}</small></h2>')
        btns = "".join(f'<button class="wkbtn" data-wk="{i}">Week of {w:%a %d %b}</button>'
                       for i, w in enumerate(weeks))
        parts.append(f'<div class="bar">{btns}<span class="sep"></span>'
                     f'<button class="wkbtn" data-wk="all">All weeks</button><span class="sep"></span>{sizer("anoms")}</div>')
        nr = len(anom_history)
        parts.append(
            f'<div class="ctl" id="an-ctl"><button class="prev" title="Older run">&larr;</button>'
            f'<input type="range" min="0" max="{nr - 1}" step="1" value="{nr - 1}">'
            f'<button class="next" title="Newer run">&rarr;</button><button class="play">Play</button>'
            f'<span class="lab"></span>'
            f'<span class="layout" style="margin-left:auto;display:flex;gap:6px">'
            f'<button data-lay="flick" class="on">Flick</button><button data-lay="sbs">Side by side</button></span></div>')
        parts.append(f'<p class="hint">Last {nr} runs for the same weeks. All maps step together; '
                     f'use the slider or &larr; / &rarr; keys, or Side by side to see the runs in a row.</p>')

        def anf(vi, wi, caption, ri=None):
            r = "" if ri is None else f' data-r="{ri}"'
            return (f'<figure class="anf" data-v="{vi}" data-w="{wi}"{r}><figcaption><span>{esc(caption)}</span>'
                    f'<a target="_blank" rel="noopener">open on ECMWF</a></figcaption>'
                    f'<img alt="{esc(caption)}"><div class="err" style="display:none"></div></figure>')

        # one week at a time
        parts.append('<div class="wk-one">')
        for wi, w in enumerate(weeks):
            flick = "".join(anf(vi, wi, f"{label} \u2013 week of {w:%d %b}")
                            for vi, (_, _, label) in enumerate(ANOMALY_MAPS))
            sbs = "".join(
                f'<h3>{esc(label)}</h3><div class="row">' +
                "".join(anf(vi, wi, run_label(bt, True) + ("  (latest)" if bt == anom_bt else ""), ri)
                        for ri, bt in enumerate(anom_history)) + '</div>'
                for vi, (_, _, label) in enumerate(ANOMALY_MAPS))
            parts.append(f'<div class="wk"><div class="row two an-flick">{flick}</div>'
                         f'<div class="an-sbs">{sbs}</div>'
                         f'{notes_html(f"anom:{anom_bt:%Y%m%d%H}:{w:%Y%m%d}", f"Notes - week of {w:%a %d %b}")}</div>')
        parts.append('</div>')
        # all weeks: one strip per variable (shows the run selected above)
        parts.append('<div class="wk-all">')
        for vi, (_, _, label) in enumerate(ANOMALY_MAPS):
            figs = "".join(anf(vi, wi, f"Week of {w:%d %b}") for wi, w in enumerate(weeks))
            parts.append(f'<h3>{esc(label)}</h3><div class="strip">{figs}</div>')
        parts.append(notes_html(f"anom:{anom_bt:%Y%m%d%H}:all", "Notes - all weeks / overall"))
        parts.append('</div>')

        # chart data, stored once and placed into the figures by the page script
        data = {"runs": [run_label(bt) for bt in anom_history], "cells": {}}
        for vi, (prod, proj, _) in enumerate(ANOMALY_MAPS):
            for wi, w in enumerate(weeks):
                cell = []
                for bt in anom_history:
                    src, err, _ = R[("anom", prod, w, bt)]
                    p = {"base_time": bt, "projection": proj, "valid_time": w}
                    cell.append({"s": src, "e": err, "l": site_url(prod, p)})
                data["cells"][f"{vi}|{wi}"] = cell
        parts.append('<script type="application/json" id="anom-data">'
                     + json.dumps(data).replace("</", "<\\/") + '</script>')
        parts.append('</section>')

    # Hovmollers
    parts.append('<section id="hov"><h2>Hovmöller diagrams')
    if hov_latest is None:
        parts.append('</h2><p class="err">No Hovmöller run found.</p></section>')
    else:
        parts.append(f' <small>latest run {run_label(hov_latest)}</small></h2>'
                     f'<div class="bar"><span class="hint" style="margin:0">Size</span>{sizer("hov")}</div>'
                     f'<p class="hint">Last {len(hov_history)} runs. Both diagrams step together; '
                     f'use the slider or &larr; / &rarr; keys.</p><div class="row two">')
        for i, (params, label) in enumerate(HOVMOLLERS):
            runs = []
            for bt in hov_history:
                p = dict(params, base_time=bt)
                runs.append((bt, site_url(HOVMOLLER_PRODUCT, p), R[("hov", i, bt)]))
            parts.append(f'<div>{viewer_html("hov", label, runs, hov_latest)}</div>')
        parts.append('</div>')
        parts.append(notes_html(f"hov:{hov_latest:%Y%m%d%H}", "Notes - Hovm\u00f6llers"))
        parts.append('</section>')
    return "\n".join(parts)


def runset_html(sid, st):
    """One run slider driving a set of charts (flick), or each chart's versions in a row (side by side)."""
    n, items = st["n"], st["items"]
    def fig(i, k=None):
        it = items[i]
        crop = f' data-crop="{",".join(str(c) for c in it["crop"])}"' if it.get("crop") else ""
        kk = "" if k is None else f' data-k="{k}"'
        return (f'<figure class="rsf" data-i="{i}"{kk}{crop}><figcaption><span>{esc(it["label"])}'
                f'<span class="when"></span></span><a target="_blank" rel="noopener">source</a></figcaption>'
                f'<img alt="{esc(it["label"])}"><div class="err" style="display:none"></div></figure>')
    flick = "".join(fig(i) for i in range(len(items)))
    sbs = "".join(f'<h3>{esc(it["label"])}</h3><div class="row">' +
                  "".join(fig(i, k) for k in reversed(range(n))) + '</div>'
                  for i, it in enumerate(items))
    ctl = ""
    if n > 1:
        ctl = (f'<div class="ctl rs-ctl"><button class="prev" title="Older">&larr;</button>'
               f'<input type="range" min="0" max="{n - 1}" step="1" value="{n - 1}">'
               f'<button class="next" title="Newer">&rarr;</button><button class="play">Play</button>'
               f'<span class="lab"></span><span class="layout" style="margin-left:auto;display:flex;gap:6px">'
               f'<button data-lay="flick" class="on">Flick</button><button data-lay="sbs">Side by side</button></span></div>')
    data = json.dumps({"n": n, "items": [{"v": it["v"]} for it in items]}).replace("</", "<\\/")
    return (f'<section class="rs" id="rs-{sid}"><h2>{esc(st["title"])}</h2>'
            f'<div class="bar"><span class="hint" style="margin:0">Size</span>{sizer("rs-" + sid)}</div>{ctl}'
            f'<div class="row rs-flick">{flick}</div><div class="rs-sbs">{sbs}</div>'
            f'<script type="application/json" class="rs-data">{data}</script></section>')


def tele_tab(sets):
    out = [runset_html(st["id"], sets[st["id"]]) for st in TELE_SETS if st["id"] in sets]
    out.append('<p class="hint">Charts with a fixed web address (CPC) are overwritten by their site, so this page keeps its own '
               'copies: "previous" versions build up over the first few days of running.</p>')
    return "\n".join(out)


def strat_tab(strat_bts, R, sets=None):
    parts = ['<section><h2>ECMWF</h2><div class="row two">']
    for i, (prod, params, label) in enumerate(STRAT_CHARTS):
        bt = strat_bts[i]
        if bt is None:
            parts.append(f'<figure><figcaption>{esc(label)}</figcaption><div class="err">No run found.</div></figure>')
            continue
        p = dict(params, base_time=bt)
        parts.append(figure_html(f"{label} – run {run_label(bt, True)}", site_url(prod, p),
                                 R[("strat", i, bt)], label))
    parts.append('</div></section>')
    for st in STRAT_SETS:
        if sets and st["id"] in sets:
            parts.append(runset_html(st["id"], sets[st["id"]]))
    return "\n".join(parts)


def build_html(ctx, R):
    title_run = ctx["met_latest"].strftime("%Y%m%d %HZ") if ctx["met_latest"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ECMWF charts {title_run}</title>
<style>{CSS}</style></head>
<body data-tab="met">
<header>
  <div class="top"><h1>ECMWF charts</h1>
  <span class="meta">built {datetime.now(LOCAL_TZ):%a %d %b %H:%M AEST}</span>
  <button class="savebtn" id="savecopy" title="Download a copy of this page with your notes written into it">Save copy with notes</button></div>
  <div class="tabs">
    <button data-tab="met" class="on">Meteograms</button>
    <button data-tab="ext">Extended Outlook</button>
    <button data-tab="tele">Teleconnections</button>
    <button data-tab="strat">Stratosphere</button>
  </div>
</header>
<main>
<div class="tab" id="tab-met">{meteogram_tab(ctx["met_latest"], ctx["met_history"], R)}</div>
<div class="tab" id="tab-ext">{extended_tab(ctx["anom_bt"], ctx["weeks"], ctx["anom_history"], ctx["hov_latest"], ctx["hov_history"], R)}</div>
<div class="tab" id="tab-tele">{tele_tab(ctx["sets"])}</div>
<div class="tab" id="tab-strat">{strat_tab(ctx["strat_bts"], R, ctx["sets"])}</div>
</main>
<footer>Charts &copy; ECMWF, licensed under CC BY 4.0 &middot; charts.ecmwf.int</footer>
<div id="zoom"><img alt=""></div>
<script>{JS}</script>
</body></html>
"""


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-time", help="meteogram run to use, YYYYMMDDHH in UTC (default: latest available)")
    ap.add_argument("--history", type=int, default=DEFAULT_HISTORY,
                    help=f"meteogram runs to compare, incl. latest (default {DEFAULT_HISTORY})")
    ap.add_argument("--out", help="output HTML path (default: ens_grams_<run>.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    ap.add_argument("--no-cache", action="store_true", help="re-download every chart, ignoring saved copies")
    ap.add_argument("--split", action="store_true",
                    help="save charts as separate image files in a 'charts' folder next to the page "
                         "(lighter for phones / web hosting) instead of inside the HTML")
    args = ap.parse_args()
    USE_CACHE[0] = not args.no_cache
    prune_cache()

    print("Finding latest runs...")
    name0, lat0, lon0 = STATIONS[0]

    # meteograms (00/12 UTC)
    def met_params(bt, eg, name=name0, lat=lat0, lon=lon0):
        return {"base_time": bt, "epsgram": eg, "lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "station_name": name}
    if args.base_time:
        met_latest = datetime.strptime(args.base_time, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    else:
        met_latest = find_latest("Meteograms", recent_runs(12, 6),
                                 lambda bt: exists("opencharts_meteogram", met_params(bt, METEOGRAMS[-1][0])))
    met_history = ([met_latest - timedelta(hours=12 * i) for i in range(max(1, args.history))][::-1]
                   if met_latest else [])

    # weekly anomalies: find the latest run, then which weeks exist (tested on the first map)
    prod0, proj0, _ = ANOMALY_MAPS[0]

    def anom_ok(bt, w):
        return exists(prod0, {"base_time": bt, "projection": proj0, "valid_time": w})

    anom_bt = find_latest("Weekly anomalies", recent_runs(EXTENDED_STEP_HOURS, 7),
                          lambda bt: anom_ok(bt, mondays_after(bt)[2]))
    weeks = []
    for w in (mondays_after(anom_bt) if anom_bt else []):
        if len(weeks) >= ANOMALY_WEEKS:
            break
        if anom_ok(anom_bt, w):
            weeks.append(w)
    anom_history = ([anom_bt - timedelta(hours=EXTENDED_STEP_HOURS * i) for i in range(max(1, ANOMALY_HISTORY))][::-1]
                    if anom_bt else [])
    if anom_bt:
        print(f"  Weekly anomalies: {len(weeks)} weeks ({', '.join(f'{w:%d %b}' for w in weeks)})")

    # Hovmollers
    hov_latest = find_latest("Hovmoller", recent_runs(EXTENDED_STEP_HOURS, 7),
                             lambda bt: exists(HOVMOLLER_PRODUCT, dict(HOVMOLLERS[0][0], base_time=bt)))
    hov_history = ([hov_latest - timedelta(hours=EXTENDED_STEP_HOURS * i) for i in range(HOVMOLLER_HISTORY)][::-1]
                   if hov_latest else [])

    # stratosphere
    strat_bts = [find_latest(label, recent_runs(EXTENDED_STEP_HOURS, 7),
                             lambda bt, prod=prod, params=params: exists(prod, dict(params, base_time=bt)))
                 for prod, params, label in STRAT_CHARTS]

    # everything to fetch: key -> (product, params)
    jobs = {}
    for name, lat, lon in STATIONS:
        if met_latest:
            for eg, _ in METEOGRAMS:
                jobs[("met", name, eg, met_latest)] = ("opencharts_meteogram", met_params(met_latest, eg, name, lat, lon))
        for bt in met_history:
            jobs[("met", name, HISTORY_PRODUCT, bt)] = ("opencharts_meteogram", met_params(bt, HISTORY_PRODUCT, name, lat, lon))
    for bt in anom_history:
        for w in weeks:
            for prod, proj, _ in ANOMALY_MAPS:
                jobs[("anom", prod, w, bt)] = (prod, {"base_time": bt, "projection": proj, "valid_time": w})
    for i, (params, _) in enumerate(HOVMOLLERS):
        for bt in hov_history:
            jobs[("hov", i, bt)] = (HOVMOLLER_PRODUCT, dict(params, base_time=bt))
    for i, (prod, params, _) in enumerate(STRAT_CHARTS):
        if strat_bts[i]:
            jobs[("strat", i, strat_bts[i])] = (prod, dict(params, base_time=strat_bts[i]))

    print(f"Fetching {len(jobs)} charts...")
    R = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {k: pool.submit(fetch, prod, params) for k, (prod, params) in jobs.items()}
        for k, f in futs.items():
            R[k] = f.result()
            src, err, cached = R[k]
            status = ("ok (saved copy)" if cached else "ok") if src else "FAILED: " + err
            desc = " ".join(f"{x:%d %b %HZ}" if isinstance(x, datetime) else str(x) for x in k)
            print(f"  {desc:<60} {status}")

    print("Fetching charts from other sites...")
    sets = collect_sets(TELE_SETS + STRAT_SETS)

    ctx = dict(sets=sets, met_latest=met_latest, met_history=met_history, anom_bt=anom_bt, weeks=weeks, anom_history=anom_history,
               hov_latest=hov_latest, hov_history=hov_history, strat_bts=strat_bts)
    stamp = (met_latest or datetime.now(timezone.utc)).strftime("%Y%m%d%H")
    out = Path(args.out or f"ens_grams_{stamp}.html").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.split:
        R = split_images(R, out.parent / "charts")
        split_sets(sets, out.parent / "charts")
    out.write_text(build_html(ctx, R), encoding="utf-8")
    ok = sum(1 for v in R.values() if v[0])
    print(f"Wrote {out}  ({ok}/{len(R)} charts)")
    if not args.no_open:
        webbrowser.open(out.as_uri())
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
