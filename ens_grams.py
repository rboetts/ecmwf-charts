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

DEFAULT_HISTORY = 4                # meteogram runs to compare (10- and 15-day), incl. the latest (12 h apart)

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
         "url": "https://www.bom.gov.au/clim_data/IDCK000080/sam_index.daily.{date}.png", "max_age_days": 10,
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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ens-grams-script/2.0; personal chart viewer)"}
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


# Some sites don't send their "intermediate" certificate. Browsers fetch it
# automatically (from the address written in the site's certificate); this does
# the same, and the full chain is still checked against the trusted roots.
_SSL_CTX = {}


def _aia_issuer_urls(der):
    out, marker, i = [], b"\x06\x08\x2b\x06\x01\x05\x05\x07\x30\x02\x86", 0   # id-ad-caIssuers, URI
    while True:
        i = der.find(marker, i)
        if i < 0:
            return out
        j = i + len(marker)
        n, j = der[j], j + 1
        if n & 0x80:                          # long-form length
            k = n & 0x7F
            n, j = int.from_bytes(der[j:j + k], "big"), j + k
        out.append(der[j:j + n].decode("ascii", "replace"))
        i += len(marker)


def _der_tlv(b, i):
    """(tag, start of value, end of value) for the DER element at i."""
    tag, n, j = b[i], b[i + 1], i + 2
    if n & 0x80:
        k = n & 0x7F
        n, j = int.from_bytes(b[j:j + k], "big"), j + k
    return tag, j, j + n


def _issuer_subject(der):
    _, s, _ = _der_tlv(der, 0)          # Certificate
    _, i, _ = _der_tlv(der, s)          # TBSCertificate
    if der[i] == 0xA0:                  # [0] version
        i = _der_tlv(der, i)[2]
    fields = []
    for _ in range(5):                  # serial, signature alg, issuer, validity, subject
        t, a, e = _der_tlv(der, i)
        fields.append(der[i:e])
        i = e
    return fields[2], fields[4]


def _ssl_context_for(host, port=443):
    if host in _SSL_CTX:
        return _SSL_CTX[host]
    import socket
    import ssl
    peek = ssl.create_default_context()
    peek.check_hostname = False
    peek.verify_mode = ssl.CERT_NONE            # only to read the site's certificate, not to trust it
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with peek.wrap_socket(sock, server_hostname=host) as ss:
            leaf = ss.getpeercert(binary_form=True)
    ctx = ssl.create_default_context()
    # the fetched certificate must itself chain to a trusted root: never let it act as a root
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    added = 0
    for url in _aia_issuer_urls(leaf):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT) as r:
                der = r.read()
            issuer, subject = _issuer_subject(der)
            if issuer == subject:
                continue                        # self-signed: refuse
            ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
            added += 1
        except Exception:
            continue
    if not added:
        raise RuntimeError(f"{host}: certificate chain incomplete and the missing certificate could not be fetched")
    print(f"    {host}: fetched the site's missing intermediate certificate")
    _SSL_CTX[host] = ctx
    return ctx


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
            parts = urllib.parse.urlsplit(url)
            host, port = parts.hostname, parts.port or 443
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX.get(host)) as r:
                    return r.read(), r.headers
            except urllib.error.URLError as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e) or host in _SSL_CTX:
                    raise
                ctx = _ssl_context_for(host, port)
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
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
    in_use = {e["file"] for e in keep}
    for old in index[:-max(n, 1)]:
        if old["file"] in in_use:
            continue
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
            versions.append({"s": src, "e": None, "lab": lab, "l": item.get("page", item["url"]), "t": ent["time"]})
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
                             "lab": f"issued {d:%a %d %b}", "l": item.get("page", url), "t": d.isoformat()})
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
            items.append({"label": it["label"], "crop": it.get("crop"), "v": versions,
                          "max_age_days": it.get("max_age_days", 4)})
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
# Page
# --------------------------------------------------------------------------
# Python builds a small "model" of tabs > sections > charts > versions; the
# page script turns it into the page. Every section works the same way.

CSS = r"""
:root { --bg:#f5f6f8; --card:#fff; --ink:#1b1f24; --muted:#5d6672; --line:#dde1e6; --accent:#1f5fa8; --accent-ink:#fff;
        --ok:#1e7b34; --warn:#a15c00; --bad:#b3261e; --chip:#eef1f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171b; --card:#1d2126; --ink:#e6e9ed; --muted:#9aa3ad; --line:#2d333a; --accent:#6ea8ec; --accent-ink:#0d1117;
          --ok:#5cc87a; --warn:#f0b357; --bad:#ff8a80; --chip:#262b31; }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--ink);
       --col:460px; --col-all:360px; }
body.size-s { --col:330px; --col-all:260px; }
body.size-l { --col:680px; --col-all:520px; }
button { font:inherit; color:inherit; }

/* header */
header { position:sticky; top:0; z-index:20; background:var(--card); border-bottom:1px solid var(--line); }
.hrow { display:flex; align-items:center; gap:8px 12px; padding:8px 16px 0; flex-wrap:wrap; }
.hrow h1 { font-size:16px; margin:0; letter-spacing:.2px; }
.status { border:1px solid var(--line); background:var(--chip); border-radius:999px; padding:2px 10px; font-size:12.5px; cursor:pointer; }
.status .short { display:none; }
.status.ok b { color:var(--ok); } .status.warn b { color:var(--warn); }
.hright { margin-left:auto; display:flex; gap:6px; align-items:center; }
.seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
.seg button { border:0; background:transparent; padding:3px 9px; font-size:12.5px; cursor:pointer; }
.seg button.on { background:var(--accent); color:var(--accent-ink); }
.btn { border:1px solid var(--line); background:transparent; border-radius:999px; padding:3px 10px; font-size:12.5px; cursor:pointer; }
.tabs { display:flex; gap:2px; padding:0 10px; overflow-x:auto; scrollbar-width:none; }
.tabs::-webkit-scrollbar { display:none; }
.tabs button { border:0; background:transparent; padding:8px 10px 7px; border-bottom:3px solid transparent; color:var(--muted);
               cursor:pointer; white-space:nowrap; font-size:14px; }
.tabs button.on { color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
#problems { display:none; padding:6px 16px 10px; font-size:13px; border-top:1px solid var(--line); max-height:40vh; overflow:auto; }
#problems.open { display:block; }
#problems li { margin:2px 0; } #problems a { color:var(--accent); cursor:pointer; }

/* tab body */
main { padding:12px 16px 24px; max-width:1900px; margin:0 auto; }
.jump { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:0 0 12px; font-size:13px; }
.jump a, .chip { border:1px solid var(--line); background:var(--card); border-radius:999px; padding:3px 10px; font-size:13px;
                 cursor:pointer; color:var(--ink); text-decoration:none; white-space:nowrap; }
.chip.on { background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }
.jump .sp { flex:1; }
section.sec { background:transparent; margin:0 0 18px; scroll-margin-top:96px; }
.shead { display:flex; align-items:baseline; gap:8px; cursor:pointer; user-select:none; padding:4px 0; }
.shead h2 { font-size:17px; margin:0; }
.shead small { color:var(--muted); font-size:12.5px; font-weight:400; }
.shead .chev { width:18px; color:var(--muted); transition:transform .15s; display:inline-block; }
section.collapsed .chev { transform:rotate(-90deg); }
section.collapsed .sbody { display:none; }
.bar { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:4px 0 10px; }
.bar .gap { width:10px; }
.bar .lab { color:var(--muted); font-size:12.5px; margin-right:2px; }
.bar .right { margin-left:auto; }
.grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fill, min(var(--col), 100%)); }
.rowh { font-size:14px; margin:10px 0 6px; font-weight:600; }
.grid.all { grid-template-columns:repeat(auto-fill, min(var(--col-all), 100%)); }
figure { margin:0; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:6px; min-width:0; }
figcaption { display:flex; gap:8px; justify-content:space-between; align-items:baseline; font-size:13px; margin:0 2px 4px; }
figcaption .t { font-weight:600; } figcaption .v { color:var(--muted); font-size:12px; display:block; font-weight:400; }
figcaption a { color:var(--muted); text-decoration:none; font-size:13px; padding:0 2px; }
figure img { display:block; width:100%; height:auto; border-radius:4px; background:#fff; cursor:zoom-in; }
.miss { display:flex; align-items:center; justify-content:center; text-align:center; min-height:120px; padding:12px;
        color:var(--muted); font-size:13px; background:var(--chip); border-radius:4px; }
.miss.bad { color:var(--bad); }
.hint { color:var(--muted); font-size:12.5px; margin:6px 0 0; }
.notes { margin:12px 0 0; }
.notes label { display:flex; justify-content:space-between; font-size:13px; font-weight:600; margin-bottom:4px; }
.notes .saved { font-weight:400; color:var(--muted); }
.notes textarea { width:100%; min-height:80px; resize:vertical; font:inherit; font-size:14px; padding:8px 10px;
                  border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink); }
.hide-d10 .it-d10, .hide-d15 .it-d15 { display:none; }

/* full-screen viewer */
#lb { position:fixed; inset:0; z-index:50; background:#0b0d10; color:#eef1f4; display:none; flex-direction:column; }
#lb.open { display:flex; }
#lb .top { display:flex; align-items:center; gap:10px; padding:8px 12px; }
#lb .ttl { font-weight:600; font-size:14px; } #lb .sub { color:#a8b0ba; font-size:12.5px; }
#lb .x { margin-left:auto; }
#lb button { background:rgba(255,255,255,.1); border:1px solid rgba(255,255,255,.18); color:#eef1f4; border-radius:999px;
             padding:4px 11px; font-size:13px; cursor:pointer; }
#lb button.on { background:#eef1f4; color:#111; }
#lb .stage { flex:1; overflow:auto; display:flex; align-items:center; justify-content:center; touch-action:pan-x pan-y pinch-zoom; }
#lb .stage img { max-width:100%; max-height:100%; background:#fff; cursor:zoom-in; }
#lb.zoom .stage { align-items:flex-start; justify-content:flex-start; }
#lb.zoom .stage img { max-width:none; max-height:none; cursor:zoom-out; }
#lb .bot { display:flex; gap:6px; align-items:center; justify-content:center; flex-wrap:wrap; padding:8px 12px 12px; }
#lb .miss { background:transparent; color:#a8b0ba; }
footer { color:var(--muted); font-size:12px; text-align:center; padding:4px 16px 20px; }

@media (max-width:600px) {
  .hrow { padding:6px 12px 0; } .hrow h1 { font-size:15px; }
  .hright .lab { display:none; }
  .status .full { display:none; } .status .short { display:inline; }
  main { padding:10px 10px 20px; }
  .shead h2 { font-size:16px; }
  section.sec { scroll-margin-top:90px; }
}
"""

JS = r"""
const M = JSON.parse(document.getElementById('model').textContent);
const IMG = M.imgs;
const $ = (s, el = document) => el.querySelector(s), $$ = (s, el = document) => [...el.querySelectorAll(s)];
const store = {
  get(k, d) { try { const v = localStorage.getItem('mc:' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('mc:' + k, JSON.stringify(v)); } catch (e) {} }
};
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e[k] = v; else e.setAttribute(k, v === true ? '' : v);
  }
  kids.flat().forEach(c => { if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : document.createTextNode(c)); });
  return e;
};

// ---- state -------------------------------------------------------------
const SEC = {};        // id -> {def, tab, ver, page, mode, timer, el}
const GROUP = {};      // sync group -> [section ids]
M.tabs.forEach(t => t.sections.forEach(s => {
  SEC[s.id] = { def: s, tab: t.id, ver: 0, page: 0, mode: 'one', timer: null, el: null };
  if (s.sync) (GROUP[s.sync] = GROUP[s.sync] || []).push(s.id);
}));
const collapsed = new Set(store.get('collapsed', []));
const savedNotes = (() => { try { return JSON.parse(document.getElementById('saved-notes').textContent || '{}'); } catch (e) { return {}; } })();

function peers(id) { const g = SEC[id].def.sync; return g ? GROUP[g] : [id]; }
function nver(s) { return s.def.versions.length; }
function items(s) { return s.def.pages[s.page].items; }

// ---- charts --------------------------------------------------------------
function chart(s, itemIdx, k, opts = {}) {
  const it = items(s)[itemIdx], v = it.v[k];
  const sub = v && v.lab ? v.lab : (s.def.versions[k] ? s.def.versions[k].long : '');
  const cap = h('figcaption', {},
    h('span', {}, h('span', { class: 't' }, opts.title || it.label), h('span', { class: 'v' }, sub)),
    v && v.l ? h('a', { href: v.l, target: '_blank', rel: 'noopener', title: 'Open at the source' }, '↗') : null);
  let body;
  if (v && v.i !== null && v.i !== undefined) {
    body = h('img', { src: IMG[v.i], alt: it.label, loading: 'lazy', 'data-crop': it.crop || null });
    body.onclick = () => openLB(s.def.id, itemIdx, k);
  } else {
    const msg = !v ? 'No earlier version saved yet' : 'Not available' + (v.e ? ': ' + v.e : '');
    body = h('div', { class: 'miss' + (k === 0 ? ' bad' : '') }, msg);
  }
  return h('figure', { class: it.cls ? 'it-' + it.cls : null }, cap, body);
}

function renderGrid(id) {
  const s = SEC[id], box = $('.charts', s.el); box.innerHTML = '';
  if (s.mode === 'one' || nver(s) < 2) {
    box.append(h('div', { class: 'grid' }, items(s).map((_, i) => chart(s, i, s.ver))));
  } else {
    items(s).forEach((it, i) => {
      const row = h('div', { class: it.cls ? 'it-' + it.cls : null }, h('div', { class: 'rowh' }, it.label),
        h('div', { class: 'grid all' }, [...Array(nver(s)).keys()].reverse().map(k =>
          chart(s, i, k, { title: s.def.versions[k].short }))));
      box.append(row);
    });
  }
  // version + mode buttons
  $$('.vbtn', s.el).forEach(b => b.classList.toggle('on', +b.dataset.k === s.ver && s.mode === 'one'));
  $$('.mbtn', s.el).forEach(b => b.classList.toggle('on', b.dataset.m === s.mode));
  $$('.pbtn', s.el).forEach(b => b.classList.toggle('on', +b.dataset.p === s.page));
  const play = $('.play', s.el); if (play) play.classList.toggle('on', !!s.timer);
  renderNotes(id);
}

function setVer(id, k, fromPlay) {
  peers(id).forEach(pid => {
    const p = SEC[pid]; if (!fromPlay && p.timer) stopPlay(pid);
    p.ver = Math.max(0, Math.min(nver(p) - 1, k)); p.mode = 'one'; if (p.el) renderGrid(pid);
  });
}
function setMode(id, m) { peers(id).forEach(pid => { SEC[pid].mode = m; stopPlay(pid); if (SEC[pid].el) renderGrid(pid); }); }
function stopPlay(id) { const s = SEC[id]; if (s.timer) { clearInterval(s.timer); s.timer = null; } }
function togglePlay(id) {
  const s = SEC[id];
  if (s.timer) { peers(id).forEach(stopPlay); renderGrid(id); return; }
  let k = nver(s) - 1; setVer(id, k);
  s.timer = setInterval(() => { k = k <= 0 ? nver(s) - 1 : k - 1; setVer(id, k, true); }, 1300);
  renderGrid(id);
}

// ---- notes -------------------------------------------------------------
function renderNotes(id) {
  const s = SEC[id], box = $('.notesbox', s.el); if (!box) return;
  const n = s.def.pages[s.page].notes || s.def.notes;
  box.innerHTML = '';
  if (!n) return;
  const key = 'notes:' + n.key;
  const ta = h('textarea', { placeholder: 'Your thoughts…' });
  let legacy = null; try { legacy = localStorage.getItem('ensnotes:' + n.key); } catch (e) {}
  ta.value = store.get(key, null) ?? (legacy || null) ?? savedNotes[n.key] ?? '';
  const saved = h('span', { class: 'saved' });
  let tm;
  ta.oninput = () => { clearTimeout(tm); tm = setTimeout(() => {
    store.set(key, ta.value); savedNotes[n.key] = ta.value; saved.textContent = 'saved';
    setTimeout(() => saved.textContent = '', 1500); }, 400); };
  box.append(h('div', { class: 'notes' }, h('label', {}, h('span', {}, n.title), saved), ta));
}

// ---- sections & tabs -----------------------------------------------------
function buildSection(sd) {
  const s = SEC[sd.id];
  const bar = h('div', { class: 'bar' });
  if (sd.pages.length > 1) {
    sd.pages.forEach((p, i) => bar.append(h('button', { class: 'chip pbtn', 'data-p': i,
      onclick: () => { s.page = i; renderGrid(sd.id); } }, p.label)));
    bar.append(h('span', { class: 'gap' }));
  }
  if (sd.versions.length > 1) {
    bar.append(h('span', { class: 'lab' }, 'Run'));
    sd.versions.forEach((v, k) => bar.append(h('button', { class: 'chip vbtn', 'data-k': k, title: v.long,
      onclick: () => setVer(sd.id, k) }, v.short)));
    bar.append(h('button', { class: 'chip play', title: 'Play through the runs', onclick: () => togglePlay(sd.id) }, '▶'));
    bar.append(h('span', { class: 'seg right' },
      h('button', { class: 'mbtn', 'data-m': 'one', onclick: () => setMode(sd.id, 'one') }, 'One run'),
      h('button', { class: 'mbtn', 'data-m': 'all', onclick: () => setMode(sd.id, 'all') }, 'All runs')));
  }
  const head = h('div', { class: 'shead', onclick: () => {
      el.classList.toggle('collapsed');
      if (el.classList.contains('collapsed')) collapsed.add(sd.id); else collapsed.delete(sd.id);
      store.set('collapsed', [...collapsed]); } },
    h('span', { class: 'chev' }, '▾'), h('h2', {}, sd.title), sd.sub ? h('small', {}, sd.sub) : null);
  const el = h('section', { class: 'sec' + (collapsed.has(sd.id) ? ' collapsed' : ''), id: sd.id }, head,
    h('div', { class: 'sbody' }, bar.childNodes.length ? bar : null, h('div', { class: 'charts' }),
      sd.hint ? h('p', { class: 'hint' }, sd.hint) : null, h('div', { class: 'notesbox' })));
  s.el = el;
  renderGrid(sd.id);
  return el;
}

function buildTab(t) {
  const app = $('#app'); app.innerHTML = '';
  app.className = '';
  const jump = h('div', { class: 'jump' });
  if (t.sections.length > 1) t.sections.forEach(sd => jump.append(h('a', { onclick: () => {
    const el = document.getElementById(sd.id); el.classList.remove('collapsed'); collapsed.delete(sd.id);
    store.set('collapsed', [...collapsed]); el.scrollIntoView({ behavior: 'smooth' }); } }, sd.title)));
  if (t.filters) {
    const f = store.get('filter:' + t.id, 0);
    const seg = h('span', { class: 'seg' });
    const apply = i => { app.classList.remove('hide-d10', 'hide-d15'); (t.filters[i].hide || []).forEach(c => app.classList.add('hide-' + c));
      $$('button', seg).forEach((b, j) => b.classList.toggle('on', i === j)); store.set('filter:' + t.id, i); };
    t.filters.forEach((fl, i) => seg.append(h('button', { onclick: () => apply(i) }, fl.label)));
    jump.append(h('span', { class: 'gap' }), seg); setTimeout(() => apply(f));
  }
  if (t.sections.length > 1) {
    jump.append(h('span', { class: 'sp' }),
      h('a', { onclick: () => { t.sections.forEach(sd => { collapsed.add(sd.id); document.getElementById(sd.id).classList.add('collapsed'); }); store.set('collapsed', [...collapsed]); } }, 'Collapse all'),
      h('a', { onclick: () => { t.sections.forEach(sd => { collapsed.delete(sd.id); document.getElementById(sd.id).classList.remove('collapsed'); }); store.set('collapsed', [...collapsed]); } }, 'Expand all'));
  }
  if (jump.childNodes.length) app.append(jump);
  t.sections.forEach(sd => app.append(buildSection(sd)));
}

let curTab = null;
function setTab(id, then) {
  Object.values(SEC).forEach(s => { stopPlay(s.def.id); s.el = null; });
  curTab = M.tabs.find(t => t.id === id) || M.tabs[0];
  $$('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === curTab.id));
  buildTab(curTab); store.set('tab', curTab.id);
  window.scrollTo(0, 0);
  if (then) setTimeout(then, 30);
}

// ---- header ----------------------------------------------------------------
function buildHeader() {
  const tabs = $('.tabs');
  M.tabs.forEach(t => tabs.append(h('button', { 'data-t': t.id, onclick: () => setTab(t.id) }, t.title)));
  const st = $('.status'), probs = M.problems;
  st.className = 'status ' + (probs.length ? 'warn' : 'ok');
  st.innerHTML = '';
  st.append(h('b', {}, probs.length ? '⚠ ' + probs.length + (probs.length === 1 ? ' issue' : ' issues') : '✓'),
    ' ', h('span', { class: 'full' }, M.count + ' charts · ' + M.built), h('span', { class: 'short' }, M.count + ' · ' + M.built_short));
  const box = $('#problems');
  if (probs.length) {
    const ul = h('ul', { style: 'margin:0;padding-left:18px' });
    probs.forEach(p => ul.append(h('li', {}, h('a', { onclick: () => { box.classList.remove('open');
      setTab(p.tab, () => { const el = document.getElementById(p.sec); if (el) { el.classList.remove('collapsed'); el.scrollIntoView(); } }); } }, p.where), ': ' + p.what)));
    box.append(ul);
    st.onclick = () => box.classList.toggle('open');
  } else st.title = 'Everything fetched and up to date';
  const size = store.get('size', 'm'), seg = $('#size');
  const setSize = z => { document.body.classList.remove('size-s', 'size-l'); if (z !== 'm') document.body.classList.add('size-' + z);
    $$('button', seg).forEach(b => b.classList.toggle('on', b.dataset.z === z)); store.set('size', z); };
  $$('button', seg).forEach(b => b.onclick = () => setSize(b.dataset.z)); setSize(size);
  $('#savecopy').onclick = saveCopy;
}

// ---- full-screen viewer ----------------------------------------------------
const LB = { sec: null, item: 0, k: 0 };
function openLB(secId, item, k) { Object.assign(LB, { sec: secId, item, k }); $('#lb').classList.add('open'); $('#lb').classList.remove('zoom'); drawLB(); }
function closeLB() { $('#lb').classList.remove('open'); }
function drawLB() {
  const s = SEC[LB.sec], its = items(s), it = its[LB.item], v = it.v[LB.k], n = nver(s);
  $('#lb .ttl').textContent = it.label;
  $('#lb .sub').textContent = (s.def.versions[LB.k] ? s.def.versions[LB.k].short + ' · ' : '') + (v && v.lab ? v.lab : (s.def.versions[LB.k] || {}).long || '');
  const stage = $('#lb .stage'); stage.innerHTML = '';
  if (v && v.i !== null && v.i !== undefined) {
    const img = h('img', { src: IMG[v.i], alt: it.label, 'data-crop': it.crop || null });
    img.onclick = () => $('#lb').classList.toggle('zoom');
    stage.append(img);
  } else stage.append(h('div', { class: 'miss' }, 'Not available' + (v && v.e ? ': ' + v.e : '')));
  const bot = $('#lb .bot'); bot.innerHTML = '';
  if (its.length > 1) bot.append(h('button', { onclick: () => stepItem(-1), title: 'Previous chart (↑)' }, '‹ Chart'));
  if (n > 1) [...Array(n).keys()].reverse().forEach(k => bot.append(h('button', { class: k === LB.k ? 'on' : null,
    onclick: () => { LB.k = k; drawLB(); } }, s.def.versions[k].short)));
  if (its.length > 1) bot.append(h('button', { onclick: () => stepItem(1), title: 'Next chart (↓)' }, 'Chart ›'));
}
function stepVer(d) { const n = nver(SEC[LB.sec]); LB.k = Math.max(0, Math.min(n - 1, LB.k - d)); drawLB(); }   // d=+1 newer
function stepItem(d) { const n = items(SEC[LB.sec]).length; LB.item = (LB.item + d + n) % n; drawLB(); }
(() => {
  let x0 = null, y0 = null;
  const st = () => $('#lb .stage');
  document.addEventListener('touchstart', e => { if (!$('#lb').classList.contains('open') || e.touches.length > 1) { x0 = null; return; }
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, { passive: true });
  document.addEventListener('touchend', e => { if (x0 === null || $('#lb').classList.contains('zoom')) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0; x0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) stepVer(dx < 0 ? 1 : -1);
    else if (Math.abs(dy) > 70 && Math.abs(dy) > 1.5 * Math.abs(dx)) stepItem(dy < 0 ? 1 : -1); }, { passive: true });
})();

// ---- keyboard ------------------------------------------------------------
function nearestSection() {
  let best = null;
  Object.values(SEC).forEach(s => { if (!s.el || nver(s) < 2 || s.el.classList.contains('collapsed')) return;
    const t = s.el.getBoundingClientRect().top; if (t < innerHeight / 2 && (!best || t > best.t)) best = { s, t }; });
  return best ? best.s : null;
}
document.addEventListener('keydown', e => {
  if (e.target.matches('textarea, input')) return;
  if ($('#lb').classList.contains('open')) {
    if (e.key === 'Escape') closeLB();
    if (e.key === 'ArrowLeft') stepVer(-1); if (e.key === 'ArrowRight') stepVer(1);
    if (e.key === 'ArrowUp') { e.preventDefault(); stepItem(-1); } if (e.key === 'ArrowDown') { e.preventDefault(); stepItem(1); }
    return;
  }
  const s = nearestSection(); if (!s) return;
  if (e.key === 'ArrowLeft') setVer(s.def.id, s.ver + 1);
  if (e.key === 'ArrowRight') setVer(s.def.id, s.ver - 1);
});

// ---- crop + trim white margins (in the browser, once per image) ------------
const trimmed = new Map(), trimmedSet = new Set();
function trim(img) {
  const src = img.currentSrc || img.src;
  if (!src || trimmedSet.has(src)) return;
  const cr = img.dataset.crop ? img.dataset.crop.split(',').map(Number) : null;
  const key = src + (cr ? '#' + cr : '');
  if (trimmed.has(key)) { img.src = trimmed.get(key); return; }
  const W = img.naturalWidth, H = img.naturalHeight; if (!W || !H) return;
  const sx = cr ? Math.round(cr[0] * W) : 0, sy = cr ? Math.round(cr[1] * H) : 0;
  const w = cr ? Math.round((cr[2] - cr[0]) * W) : W, hh = cr ? Math.round((cr[3] - cr[1]) * H) : H;
  const c = document.createElement('canvas'); c.width = w; c.height = hh;
  const cx = c.getContext('2d'); cx.drawImage(img, sx, sy, w, hh, 0, 0, w, hh);
  let d; try { d = cx.getImageData(0, 0, w, hh).data; } catch (e) { trimmedSet.add(src); return; }
  const blank = (x, y) => { const i = (y * w + x) * 4; return d[i + 3] < 10 || (d[i] > 245 && d[i + 1] > 245 && d[i + 2] > 245); };
  const rowBlank = y => { for (let x = 0; x < w; x += 2) if (!blank(x, y)) return false; return true; };
  const colBlank = (x, a, b) => { for (let y = a; y <= b; y += 2) if (!blank(x, y)) return false; return true; };
  let t = 0, b = hh - 1, l = 0, r = w - 1;
  while (t < b && rowBlank(t)) t++; while (b > t && rowBlank(b)) b--;
  while (l < r && colBlank(l, t, b)) l++; while (r > l && colBlank(r, t, b)) r--;
  t = Math.max(0, t - 6); l = Math.max(0, l - 6); b = Math.min(hh - 1, b + 6); r = Math.min(w - 1, r + 6);
  const cw = r - l + 1, ch = b - t + 1;
  if (!cr && cw * ch > 0.97 * w * hh) { trimmedSet.add(src); return; }
  const o = document.createElement('canvas'); o.width = cw; o.height = ch;
  o.getContext('2d').drawImage(c, l, t, cw, ch, 0, 0, cw, ch);
  const out = o.toDataURL('image/png');
  trimmed.set(key, out); trimmedSet.add(out); img.src = out;
}
document.addEventListener('load', e => { if (e.target.tagName === 'IMG') trim(e.target); }, true);

// ---- save a copy with notes ------------------------------------------------
function saveCopy() {
  const doc = document.documentElement.cloneNode(true);
  $('#app', doc).innerHTML = ''; $('.tabs', doc).innerHTML = ''; $('#problems', doc).innerHTML = '';
  $('#lb .stage', doc).innerHTML = '';
  const model = JSON.parse(JSON.stringify(M));
  model.imgs = model.imgs.map(u => u.startsWith('data:') ? u : new URL(u, location.href).href);
  $('#model', doc).textContent = JSON.stringify(model).replace(/<\//g, '<\\/');
  const notes = Object.assign({}, savedNotes);
  M.tabs.forEach(t => t.sections.forEach(s => [s.notes, ...s.pages.map(p => p.notes)].forEach(n => {
    if (n) { const v = store.get('notes:' + n.key, null); if (v !== null) notes[n.key] = v; } })));
  $('#saved-notes', doc).textContent = JSON.stringify(notes).replace(/<\//g, '<\\/');
  const a = h('a', { download: (location.pathname.split('/').pop() || 'met_charts.html').replace(/(_notes)?\.html?$/i, '').replace(/^$/, 'met_charts') + '_notes.html' });
  a.href = URL.createObjectURL(new Blob(['<!doctype html>\n' + doc.outerHTML], { type: 'text/html' }));
  document.body.append(a); a.click(); a.remove();
}

// ---- go --------------------------------------------------------------------
buildHeader();
$('#lb .x').onclick = closeLB;
setTab(store.get('tab', M.tabs[0].id));
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1f5fa8">
<title>Met charts</title>
<style>__CSS__</style></head>
<body>
<header>
  <div class="hrow">
    <h1>Met charts</h1>
    <span class="status"></span>
    <span class="hright">
      <span class="lab" style="font-size:12.5px;color:var(--muted)">Size</span>
      <span class="seg" id="size"><button data-z="s">S</button><button data-z="m">M</button><button data-z="l">L</button></span>
      <button class="btn" id="savecopy" title="Download a copy of this page with your notes in it">Save notes</button>
    </span>
  </div>
  <div id="problems"></div>
  <nav class="tabs"></nav>
</header>
<main id="app"></main>
<footer>Charts &copy; ECMWF (CC BY 4.0), NOAA CPC, Bureau of Meteorology, StratObserve. Tap a chart for full screen.</footer>
<div id="lb"><div class="top"><div><div class="ttl"></div><div class="sub"></div></div><button class="x">Close ✕</button></div>
<div class="stage"></div><div class="bot"></div></div>
<script type="application/json" id="model">__MODEL__</script>
<script type="application/json" id="saved-notes">{}</script>
<script>__JS__</script>
</body></html>
"""


def _ago_label(bt, latest, step_h):
    if bt == latest:
        return "Latest"
    hrs = int(round((latest - bt).total_seconds() / 3600))
    return f"−{hrs // 24} d" if step_h >= 24 and hrs % 24 == 0 else f"−{hrs} h"


def build_model(ctx, R):
    now = datetime.now(timezone.utc)
    imgs, index = [], {}

    def reg(src):
        if src not in index:
            index[src] = len(imgs)
            imgs.append(src)
        return index[src]

    def V(res, link, lab=None):
        src, err = res[0], res[1]
        return {"i": reg(src) if src else None, "e": None if src else err, "l": link, "lab": lab}

    def run_versions(runs, step_h):            # runs newest first
        return [{"short": _ago_label(bt, runs[0], step_h), "long": run_label(bt)} for bt in runs]

    problems = []

    def check(tab, sec, title, its, latest_time=None, max_age_h=None):
        for it in its:
            v0 = it["v"][0] if it["v"] else None
            if not v0 or v0["i"] is None:
                problems.append({"tab": tab, "sec": sec, "where": f"{title} · {it['label']}",
                                 "what": "not fetched" + (f" ({v0['e']})" if v0 and v0.get("e") else "")})
        if latest_time and max_age_h and (now - latest_time).total_seconds() / 3600 > max_age_h:
            problems.append({"tab": tab, "sec": sec, "where": title,
                             "what": f"latest run is {run_label(latest_time, True)} (older than expected)"})

    tabs = []

    # Meteograms -------------------------------------------------------------
    secs = []
    hist = list(reversed(ctx["met_history"]))           # newest first
    if hist:
        for name, lat, lon in STATIONS:
            base = {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "station_name": name}
            its = []
            for eg, label in METEOGRAMS:
                its.append({"label": label, "cls": "d10" if "10d" in eg else "d15", "v": [
                    V(R[("met", name, eg, bt)], site_url("opencharts_meteogram", dict(base, base_time=bt, epsgram=eg)))
                    for bt in hist]})
            sid = "st-" + name.lower().replace(" ", "-")
            check("met", sid, name, its, hist[0], 36)
            secs.append({"id": sid, "title": name, "sub": f"{lat:.2f}, {lon:.2f}", "sync": "met",
                         "versions": run_versions(hist, 12), "pages": [{"label": None, "items": its}]})
    tabs.append({"id": "met", "title": "Meteograms", "sections": secs,
                 "filters": [{"label": "Both"}, {"label": "10-day", "hide": ["d15"]}, {"label": "15-day", "hide": ["d10"]}]})

    # Extended Outlook -------------------------------------------------------
    secs = []
    ahist = list(reversed(ctx["anom_history"]))
    weeks, abt = ctx["weeks"], ctx["anom_bt"]
    if abt and weeks:
        def cell(prod, proj, w, label):
            return {"label": label, "v": [V(R[("anom", prod, w, bt)],
                    site_url(prod, {"base_time": bt, "projection": proj, "valid_time": w})) for bt in ahist]}
        pages = []
        for w in weeks:
            pages.append({"label": f"{w:%d %b}", "items": [cell(p, pr, w, lab) for p, pr, lab in ANOMALY_MAPS],
                          "notes": {"key": f"anom:{abt:%Y%m%d%H}:{w:%Y%m%d}", "title": f"Notes – week of {w:%a %d %b}"}})
        pages.append({"label": "All weeks",
                      "items": [cell(p, pr, w, f"{lab} · wk {w:%d %b}") for p, pr, lab in ANOMALY_MAPS for w in weeks],
                      "notes": {"key": f"anom:{abt:%Y%m%d%H}:all", "title": "Notes – all weeks"}})
        for pg in pages[:-1]:
            check("ext", "anoms", f"Weekly anomalies · week of {pg['label']}", pg["items"])
        check("ext", "anoms", "Weekly anomalies", [], ahist[0], 60)
        secs.append({"id": "anoms", "title": "Weekly mean anomalies", "sub": "week starting (Monday)",
                     "versions": run_versions(ahist, 24), "pages": pages})
    hhist = list(reversed(ctx["hov_history"]))
    if hhist:
        its = [{"label": label, "v": [V(R[("hov", i, bt)], site_url(HOVMOLLER_PRODUCT, dict(params, base_time=bt)))
                                      for bt in hhist]} for i, (params, label) in enumerate(HOVMOLLERS)]
        check("ext", "hov", "Hovmöllers", its, hhist[0], 60)
        secs.append({"id": "hov", "title": "Hovmöller diagrams", "sub": "Southern Hemisphere",
                     "versions": run_versions(hhist, 24), "pages": [{"label": None, "items": its}],
                     "notes": {"key": f"hov:{hhist[0]:%Y%m%d%H}", "title": "Notes – Hovmöllers"}})
    tabs.append({"id": "ext", "title": "Extended Outlook", "sections": secs})

    # Teleconnections + Stratosphere (sets from other sites) -------------------
    def set_section(tab, sid, st):
        n = st["n"]
        names = ["Latest", "Previous"] + [f"{k} back" for k in range(2, n)]
        its = []
        for it in st["items"]:
            vs = [{"i": reg(v["s"]) if v["s"] else None, "e": None if v["s"] else v["e"], "l": v["l"], "lab": v["lab"]}
                  for v in it["v"]]
            its.append({"label": it["label"], "crop": ",".join(str(c) for c in it["crop"]) if it.get("crop") else None, "v": vs})
            t0 = it["v"][0].get("t") if it["v"] else None
            if t0:
                age_d = (now - datetime.fromisoformat(t0)).total_seconds() / 86400
                if age_d > it.get("max_age_days", 4):
                    problems.append({"tab": tab, "sec": sid, "where": f"{st['title']} · {it['label']}",
                                     "what": f"latest is {age_d:.0f} days old"})
        check(tab, sid, st["title"], its)
        return {"id": sid, "title": st["title"], "versions": [{"short": names[k], "long": names[k]} for k in range(n)],
                "pages": [{"label": None, "items": its}],
                "hint": "Previous versions of CPC charts are saved by this page, so they build up over the first few days."
                        if any(x.get("kind") == "static" for x in next(s for s in TELE_SETS + STRAT_SETS if s["id"] == sid)["items"]) else None}

    sets = ctx["sets"]
    tabs.append({"id": "tele", "title": "Teleconnections",
                 "sections": [set_section("tele", st["id"], sets[st["id"]]) for st in TELE_SETS if st["id"] in sets]})
    secs = []
    sits = []
    for i, (prod, params, label) in enumerate(STRAT_CHARTS):
        bt = ctx["strat_bts"][i]
        if bt:
            sits.append({"label": label, "v": [V(R[("strat", i, bt)], site_url(prod, dict(params, base_time=bt)), f"run {run_label(bt, True)}")]})
            check("strat", "ecmwf-strat", "ECMWF", sits[-1:], bt, 60)
        else:
            sits.append({"label": label, "v": [{"i": None, "e": "no recent run found", "l": None, "lab": None}]})
            check("strat", "ecmwf-strat", "ECMWF", sits[-1:])
    secs.append({"id": "ecmwf-strat", "title": "ECMWF", "versions": [{"short": "Latest", "long": "Latest"}],
                 "pages": [{"label": None, "items": sits}]})
    secs += [set_section("strat", st["id"], sets[st["id"]]) for st in STRAT_SETS if st["id"] in sets]
    tabs.append({"id": "strat", "title": "Stratosphere", "sections": secs})

    return {"tabs": tabs, "imgs": imgs, "problems": problems, "count": len(imgs),
            "built": datetime.now(LOCAL_TZ).strftime("built %a %d %b %H:%M AEST"),
            "built_short": datetime.now(LOCAL_TZ).strftime("%a %H:%M")}


def split_model(model, folder):
    """Write embedded images out as files; the page then links to charts/<name>."""
    folder.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
    for i, src in enumerate(model["imgs"]):
        if src.startswith("data:"):
            head, b64 = src.split(",", 1)
            img = base64.b64decode(b64)
            name = hashlib.sha1(img).hexdigest()[:16] + ext.get(head[5:].split(";")[0], ".png")
            (folder / name).write_bytes(img)
            model["imgs"][i] = f"{folder.name}/{name}"


def build_html(model):
    data = json.dumps(model, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("__CSS__", CSS).replace("__JS__", JS).replace("__MODEL__", data)


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
            for eg, _ in METEOGRAMS:
                jobs[("met", name, eg, bt)] = ("opencharts_meteogram", met_params(bt, eg, name, lat, lon))
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
    model = build_model(ctx, R)
    if args.split:
        split_model(model, out.parent / "charts")
    out.write_text(build_html(model), encoding="utf-8")
    ok = sum(1 for v in R.values() if v[0])
    print(f"Wrote {out}  ({model['count']} charts, {len(model['problems'])} issue(s))")
    for pr in model["problems"]:
        print(f"  ! {pr['where']}: {pr['what']}")
    if not args.no_open:
        webbrowser.open(out.as_uri())
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
