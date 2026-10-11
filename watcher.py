"""Kolkata advance-booking watcher for Dune: Part Three and Avengers: Doomsday.

Runs on GitHub Actions every ~10 minutes and sends Telegram alerts when
booking opens. Sources, each checked independently:
  1. District (district.in) Kolkata film pages - real showtime data
  2. PVR INOX official booking list for Kolkata - films and IMAX variants on sale
  3. BookMyShow Kolkata pages via a real browser (Playwright) - best effort
  4. Google News RSS - India-specific booking headlines
  5. Official YouTube channels - "tickets on sale" style uploads
  6. r/IMAXIndia - community chatter, best effort
"""

import calendar
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

HERE = pathlib.Path(__file__).resolve().parent


def load_dotenv():
    """Read KEY=VALUE lines from a local .env file (used when running on a PC)."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
STOP_AFTER = date(2026, 12, 25)
# GitHub commits state.json; a PC run keeps its own file so the two never clash.
ON_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))
STATE_FILE = HERE / ("state.json" if ON_GITHUB else "state.local.json")
LOG_FILE = HERE / "watcher.log"
WATCH_NAME = os.environ.get("WATCH_NAME") or ("GitHub" if ON_GITHUB else "PC")

CITY = "kolkata"
IMAX_CINEMA = "south city"  # INOX South City Mall, Kolkata's only IMAX

FILMS = [
    {
        "key": "dune3",
        "name": "Dune: Part Three",
        "title_words": ["dune"],
        "district": "https://www.district.in/movies/dune-part-three-movie-tickets-in-kolkata-MV207332",
        "bms": "https://in.bookmyshow.com/movies/kolkata/dune-part-three/ET00491771",
        "news": [
            "Dune Part Three advance booking India",
            "Dune Part Three IMAX India booking",
            "Dune Part Three tickets PVR INOX",
        ],
    },
    {
        "key": "doomsday",
        "name": "Avengers: Doomsday",
        "title_words": ["doomsday"],
        "district": "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-kolkata-MV176735",
        "bms": "https://in.bookmyshow.com/movies/kolkata/avengers-doomsday/ET00439706",
        "news": [
            "Avengers Doomsday advance booking India",
            "Avengers Doomsday IMAX India booking",
            "Avengers Doomsday tickets PVR INOX",
        ],
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Official PVR INOX booking API (the one pvrcinemas.com itself calls).
PVR_URL = "https://api3.pvrcinemas.com/api/v1/booking/content/nowshowing"
PVR_HEADERS = {
    "Content-Type": "application/json",
    "city": "Kolkata",
    "appVersion": "1.0",
    "platform": "WEBSITE",
    "country": "INDIA",
    "chain": "PVR",
    "Origin": "https://www.pvrcinemas.com",
    "Referer": "https://www.pvrcinemas.com/",
}
PVR_SITE = "https://www.pvrcinemas.com/"

YOUTUBE_CHANNELS = {
    "Warner Bros. India": "UC2FrJXBb3WVAKZ8jpxkZxlA",
    "Marvel India": "UCah9jr4Ehlrt4Wv-q-CgJkg",
}
REDDIT_FEED = "https://www.reddit.com/r/IMAXIndia/new/.rss"

# A source must fail this many runs in a row (~1 hour) before I get warned.
BLIND_AFTER = 6
MAX_DATE_PAGES = 4
NEWS_MAX_AGE_DAYS = 3
NEWS_MAX_PER_RUN = 3

INDIA_WORDS = [
    "india", "indian", "kolkata", "mumbai", "delhi", "bengaluru", "bangalore",
    "hyderabad", "chennai", "bookmyshow", "district app", "pvr", "inox",
]
BOOKING_WORDS = [
    "advance booking", "bookings", "booking open", "booking date", "pre-sale",
    "presale", "pre-booking", "tickets go on sale", "ticket sale", "tickets live",
]
# Looser set for official channels and r/IMAXIndia, which are India-only already.
SALE_WORDS = BOOKING_WORDS + [
    "booking", "book now", "book your", "tickets on sale", "on sale", "tickets now",
    "get tickets", "tickets out", "tickets are live",
]


class Blocked(Exception):
    pass


def log(msg):
    line = f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] {msg}"
    if sys.stdout:  # None under pythonw, which the scheduled task uses
        try:
            print(line, flush=True)
        except (UnicodeEncodeError, OSError):  # Windows consoles can't print emoji
            print(line.encode("ascii", "replace").decode(), flush=True)
    if not ON_GITHUB:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
                LOG_FILE.unlink()
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


_cache = {}

# Sites that reject GitHub's servers but can be read through Google Translate's
# page relay, which fetches the page from Google's side and passes it back intact.
RELAY_HOSTS = ("district.in", "bookmyshow.com")


def relay_url(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.replace("-", "--").replace(".", "-") + ".translate.goog"
    # nc changes every minute so the relay can't hand back an old copy.
    extra = f"nc={int(time.time()) // 60}&_x_tr_sl=hi&_x_tr_tl=en&_x_tr_hl=en"
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urllib.parse.urlunsplit(("https", host, parts.path, query, ""))


def fetch(url, post=None, headers=None):
    """GET (or POST json) with browser-like headers, a timeout and one retry.

    Results are cached for the run so shared feeds are fetched once. A blocked
    GET to a relay-able site is retried once through the relay.
    """
    if url in _cache:
        return _cache[url]
    host = urllib.parse.urlsplit(url).netloc
    try:
        text = _request(url, post, headers)
    except Blocked:
        if post is not None or not host.endswith(RELAY_HOSTS):
            raise
        text = _request(relay_url(url), None, None)
        log(f"{host}: direct request blocked, read through relay")
    _cache[url] = text
    return text


def _request(url, post, headers):
    last = None
    for attempt in range(2):
        try:
            h = {**HEADERS, **(headers or {})}
            if post is None:
                r = requests.get(url, headers=h, timeout=25)
            else:
                r = requests.post(url, headers=h, json=post, timeout=25)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429, 503):
                raise Blocked(f"HTTP {r.status_code}")
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        if attempt == 0:
            time.sleep(3)
    raise last


# ---------------------------------------------------------------- state

def load_state():
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            log("state.json is corrupt, starting fresh")
        if not isinstance(state, dict):
            state = {}
    state.setdefault("alerted", {})
    state.setdefault("health", {})
    state.setdefault("last_heartbeat", None)
    state.setdefault("started", False)
    return state


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ------------------------------------------------------------- telegram

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("DRY RUN (no Telegram credentials), message not sent:\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        log(f"Telegram send failed: {type(e).__name__}")
        return False
    if not r.ok:
        log(f"Telegram send failed: HTTP {r.status_code} {r.text[:200]}")
    return r.ok


def alert_once(state, key, text):
    """Send an alert only if this key was never alerted. Unsent alerts retry next run."""
    if key in state["alerted"]:
        return
    if send_telegram(text):
        state["alerted"][key] = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        log(f"ALERT sent: {key}")


def link(url, label):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


# --------------------------------------------------------------- health

def mark_ok(state, source):
    h = state["health"].get(source, {})
    if h.get("warned"):
        send_telegram(f"✅ <b>{html.escape(source)}</b> is readable again.")
    state["health"][source] = {"status": "ok", "fails": 0, "warned": False}


def mark_failed(state, source, reason, url, warn=True):
    h = state["health"].get(source, {})
    fails = min(h.get("fails", 0) + 1, BLIND_AFTER)
    warned = h.get("warned", False)
    if warn and fails >= BLIND_AFTER and not warned:
        warned = send_telegram(
            f"⚠️ <b>Watcher is blind on {html.escape(source)}</b>\n"
            f"Reason: {html.escape(reason)}\n"
            f"It can't tell whether booking is open there. Check manually: "
            f"{link(url, 'open page')}"
        )
    state["health"][source] = {"status": reason, "fails": fails, "warned": warned}
    log(f"{source}: FAILED ({reason})")


# ------------------------------------------------------------- district

def next_data(page):
    tag = BeautifulSoup(page, "html.parser").find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        raise ValueError("no __NEXT_DATA__ in page")
    return json.loads(tag.string)["props"]["pageProps"]


def scan_district(props, film):
    """Walk District's page data and pull out show dates and real sessions."""
    found = {"is_film": False, "dates": set(), "shows": []}

    def cinema_of(node):
        info = node.get("cinemaInfo") if isinstance(node.get("cinemaInfo"), dict) else node
        name = info.get("name") or info.get("label") or "Unknown cinema"
        city = info.get("cityKey") or info.get("city") or ""
        return str(name), str(city).lower()

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and all(w in name.lower() for w in film["title_words"]):
                found["is_film"] = True
            for key in ("showDates", "sessionDates"):
                dates = node.get(key)
                if isinstance(dates, list):
                    found["dates"].update(d for d in dates if isinstance(d, str))
            sessions = node.get("sessions")
            if isinstance(sessions, list) and sessions:
                cinema, city = cinema_of(node)
                for s in sessions:
                    if not isinstance(s, dict) or not s.get("showTime"):
                        continue
                    fmt = " ".join(
                        str(s.get(k) or "") for k in ("scrnFmt", "premiumLabel", "audi")
                    )
                    found["shows"].append(
                        {
                            "cinema": cinema,
                            "city": city,
                            "time": str(s["showTime"]),
                            "format": str(s.get("scrnFmt") or "").strip(),
                            "imax": "imax" in fmt.lower(),
                        }
                    )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(props)
    return found


def check_district(state, film):
    source = f"District / {film['name']}"
    url = film["district"]
    try:
        found = scan_district(next_data(fetch(url)), film)
        if not found["is_film"]:
            raise ValueError("film not found on page (URL changed?)")
    except Blocked as e:
        return mark_failed(state, source, f"blocked ({e})", url)
    except Exception as e:
        return mark_failed(state, source, f"{type(e).__name__}: {e}"[:150], url)
    mark_ok(state, source)

    dates = sorted(found["dates"])
    shows = found["shows"]
    imax_alerted = f"{film['key']}:district:imax" in state["alerted"]

    # The page only carries sessions for one date, so look at a few more
    # dates until IMAX has been found.
    if dates and not imax_alerted and not any(s["imax"] for s in shows):
        for d in dates[:MAX_DATE_PAGES]:
            try:
                extra = scan_district(next_data(fetch(f"{url}?fromdate={d}")), film)
            except Exception as e:
                log(f"{source}: date page {d} failed ({type(e).__name__})")
                continue
            shows += extra["shows"]
            if any(s["imax"] for s in extra["shows"]):
                break

    kolkata = [s for s in shows if CITY in s["city"] or CITY in s["cinema"].lower()]
    log(f"{source}: ok, {len(dates)} show dates, {len(shows)} sessions, {len(kolkata)} in Kolkata")

    if not dates and not shows:
        return

    if kolkata or not shows:
        cinemas = sorted({s["cinema"] for s in kolkata})
        formats = sorted({s["format"] for s in kolkata if s["format"]})
        lines = [f"🎟️ <b>BOOKING OPEN — {html.escape(film['name'])}</b>", "Kolkata, on District"]
        if dates:
            lines.append("Dates: " + html.escape(", ".join(dates[:6])))
        if formats:
            lines.append("Formats: " + html.escape(", ".join(formats)))
        if cinemas:
            lines.append("Cinemas: " + html.escape("; ".join(cinemas[:6])))
        lines.append("👉 " + link(url, "Book now"))
        alert_once(state, f"{film['key']}:district:booking_open", "\n".join(lines))
    else:
        # Sessions exist but none are in Kolkata: the page fell back to another city.
        alert_once(
            state,
            f"{film['key']}:district:booking_open_elsewhere",
            f"🎟️ <b>BOOKING OPEN (not Kolkata yet) — {html.escape(film['name'])}</b>\n"
            f"District shows sessions, but none in Kolkata so far. Kolkata usually follows soon.\n"
            f"👉 {link(url, 'Check page')}",
        )

    imax = [s for s in kolkata if s["imax"]]
    if imax:
        at_south_city = any(IMAX_CINEMA in s["cinema"].lower() for s in imax)
        times = sorted({s["time"].replace("T", " ") for s in imax})
        alert_once(
            state,
            f"{film['key']}:district:imax",
            f"🎬 <b>IMAX CONFIRMED — {html.escape(film['name'])}</b>\n"
            f"{html.escape(imax[0]['cinema'])}"
            f"{'' if at_south_city else ' (not South City, double-check)'}\n"
            f"Shows: {html.escape(', '.join(times[:8]))}\n"
            f"👉 {link(url, 'Book IMAX now')}",
        )


# ---------------------------------------------------------- bookmyshow

def bms_snapshot(url):
    """Return a BookMyShow page's title, button labels and text.

    BookMyShow's Cloudflare rejects plain HTTP clients, so the usual route is
    the relay inside fetch(). If that fails too and Playwright is installed
    (PC runs), a real browser is tried.
    """
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
        return {
            "title": soup.title.get_text() if soup.title else "",
            "buttons": [el.get_text(" ", strip=True) for el in soup.find_all("button")],
            "text": soup.get_text(" "),
        }
    except Exception as plain_error:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise plain_error
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1366, "height": 768},
            ).new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:  # either marker means the film block has rendered
                page.wait_for_selector(
                    'button:has-text("Book tickets"), :text("interested")', timeout=15000
                )
            except Exception:
                pass
            return {
                "title": page.title(),
                "buttons": [t.strip() for t in page.locator("button").all_inner_texts()],
                "text": page.inner_text("body"),
            }
        finally:
            browser.close()


def check_bms(state, film):
    url = film["bms"]
    if not url:
        return
    source = f"BookMyShow / {film['name']}"
    # BookMyShow blocks datacenter IPs much of the time, so failures here never warn.
    try:
        snap = bms_snapshot(url)
    except Exception as e:
        return mark_failed(state, source, f"{type(e).__name__}: {e}"[:150], url, warn=False)
    title = snap["title"].lower()
    text = snap["text"].lower()
    if "attention required" in title or "just a moment" in title:
        return mark_failed(state, source, "blocked (Cloudflare)", url, warn=False)
    if not all(w in title for w in film["title_words"]):
        return mark_failed(state, source, "page is not this film", url, warn=False)

    live = any(b.strip().lower() == "book tickets" for b in snap["buttons"])
    if not live and "interested" not in text:
        # Neither the upcoming nor the bookable layout: don't read it as "not open".
        return mark_failed(state, source, "unrecognised page layout", url, warn=False)
    mark_ok(state, source)
    log(f"{source}: ok, {'BOOK TICKETS button live' if live else 'still upcoming (Interested)'}")
    if live:
        alert_once(
            state,
            f"{film['key']}:bms:booking_open",
            f"🎟️ <b>BOOKING OPEN — {html.escape(film['name'])}</b>\n"
            f"BookMyShow Kolkata now shows the Book tickets button.\n"
            f"👉 {link(url, 'Book on BookMyShow')} · {link(film['district'], 'District')}",
        )


# ------------------------------------------------------------- pvr inox

def check_pvr(state, film):
    source = "PVR INOX Kolkata"
    try:
        data = json.loads(fetch(PVR_URL, post={"city": "Kolkata"}, headers=PVR_HEADERS))
        groups = data["output"]["mv"]
        if not groups:
            raise ValueError("empty film list")
    except Blocked as e:
        return mark_failed(state, source, f"blocked ({e})", PVR_SITE)
    except Exception as e:
        return mark_failed(state, source, f"{type(e).__name__}: {e}"[:150], PVR_SITE)
    mark_ok(state, source)

    variants = []
    for group in groups:
        for f in group.get("films") or [group]:
            name = str(f.get("filmName") or "")
            if all(w in name.lower() for w in film["title_words"]):
                variants.append((name, str(f.get("format") or "")))
    log(f"{source} / {film['name']}: ok, {len(variants)} listed variants")
    if not variants:
        return

    names = sorted({n for n, _ in variants})
    alert_once(
        state,
        f"{film['key']}:pvr:listed",
        f"🎟️ <b>ON PVR INOX BOOKING LIST — {html.escape(film['name'])}</b>\n"
        f"Kolkata, official PVR INOX site\n"
        + "\n".join("• " + html.escape(n) for n in names[:8])
        + f"\n👉 {link(PVR_SITE, 'Book on PVR INOX')} · {link(film['district'], 'District')}",
    )
    imax = sorted({n for n, fmt in variants if "imax" in (n + " " + fmt).lower()})
    if imax:
        alert_once(
            state,
            f"{film['key']}:pvr:imax",
            f"🎬 <b>IMAX CONFIRMED — {html.escape(film['name'])}</b>\n"
            f"PVR INOX lists an IMAX version for Kolkata (INOX South City is the only IMAX):\n"
            + "\n".join("• " + html.escape(n) for n in imax[:4])
            + f"\n👉 {link(PVR_SITE, 'Book IMAX on PVR INOX')} · {link(film['district'], 'District')}",
        )


# ---------------------------------------------------- youtube and reddit

def recent_titles(feed_url, film, words):
    """Feed entries from the last few days whose title names the film and a sale word."""
    now = time.time()
    out = []
    for entry in feedparser.parse(fetch(feed_url)).entries:
        title = entry.get("title", "")
        low = title.lower()
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not stamp or now - calendar.timegm(stamp) > NEWS_MAX_AGE_DAYS * 86400:
            continue
        if all(w in low for w in film["title_words"]) and any(w in low for w in words):
            out.append((title, entry.get("link", feed_url)))
    return out


def check_youtube(state, film):
    for channel, channel_id in YOUTUBE_CHANNELS.items():
        source = f"YouTube / {channel}"
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            hits = recent_titles(url, film, SALE_WORDS)
        except Exception as e:
            mark_failed(state, source, f"{type(e).__name__}: {e}"[:150], url, warn=False)
            continue
        mark_ok(state, source)
        for title, href in hits[:2]:
            alert_once(
                state,
                f"{film['key']}:youtube:{hashlib.sha1(href.encode()).hexdigest()[:12]}",
                f"📣 <b>OFFICIAL VIDEO — {html.escape(film['name'])}</b>\n"
                f"{html.escape(channel)}: {html.escape(title)}\n"
                f"👉 {link(href, 'Watch')} · {link(film['district'], 'District page')}",
            )


def check_reddit(state, film):
    source = "Reddit / r/IMAXIndia"
    try:
        hits = recent_titles(REDDIT_FEED, film, SALE_WORDS)
    except Exception as e:
        # Reddit often refuses datacenter IPs; never warn about it.
        return mark_failed(state, source, f"{type(e).__name__}: {e}"[:150], REDDIT_FEED, warn=False)
    mark_ok(state, source)
    for title, href in hits[:2]:
        alert_once(
            state,
            f"{film['key']}:reddit:{hashlib.sha1(href.encode()).hexdigest()[:12]}",
            f"💬 <b>REDDIT CHATTER (unverified) — {html.escape(film['name'])}</b>\n"
            f"{html.escape(title)}\n"
            f"👉 {link(href, 'Open post')} · {link(film['district'], 'District page')}",
        )


# ----------------------------------------------------------------- news

def check_news(state, film):
    source = f"Google News / {film['name']}"
    now = time.time()
    hits = {}
    ok = False
    for query in film["news"]:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(f"{query} when:{NEWS_MAX_AGE_DAYS}d")
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        try:
            feed = feedparser.parse(fetch(url))
        except Exception as e:
            log(f"{source}: query failed ({type(e).__name__})")
            continue
        ok = True
        for entry in feed.entries:
            title = entry.get("title", "")
            low = title.lower()
            published = entry.get("published_parsed")
            if not published or now - calendar.timegm(published) > NEWS_MAX_AGE_DAYS * 86400:
                continue
            if not all(w in low for w in film["title_words"]):
                continue
            if not any(w in low for w in INDIA_WORDS):
                continue
            if not any(w in low for w in BOOKING_WORDS):
                continue
            # Same story is syndicated under slightly different titles; key on the words.
            norm = re.sub(r"[^a-z0-9 ]", "", low.rsplit(" - ", 1)[0])
            digest = hashlib.sha1(norm.encode()).hexdigest()[:12]
            hits.setdefault(digest, (title, entry.get("link", url)))

    if not ok:
        return mark_failed(state, source, "all queries failed", "https://news.google.com")
    mark_ok(state, source)
    log(f"{source}: ok, {len(hits)} India booking headlines")

    sent = 0
    for digest, (title, href) in hits.items():
        key = f"{film['key']}:news:{digest}"
        if key in state["alerted"]:
            continue
        if sent >= NEWS_MAX_PER_RUN:
            break
        alert_once(
            state,
            key,
            f"📅 <b>BOOKING NEWS — {html.escape(film['name'])}</b>\n"
            f"{html.escape(title)}\n"
            f"👉 {link(href, 'Read')} · {link(film['district'], 'District page')}",
        )
        sent += 1


# ----------------------------------------------------------------- main

def status_lines(state):
    lines = []
    for source, h in sorted(state["health"].items()):
        icon = "🟢" if h.get("status") == "ok" else "🔴"
        detail = "" if h.get("status") == "ok" else f" — {h.get('status')}"
        lines.append(f"{icon} {html.escape(source + detail)}")
    return "\n".join(lines)


CHECKS = {
    "district": (check_district, "District"),
    "pvr": (check_pvr, "PVR INOX"),
    "bms": (check_bms, "BookMyShow"),
    "news": (check_news, "Google News"),
    "youtube": (check_youtube, "YouTube"),
    "reddit": (check_reddit, "Reddit"),
}


def enabled_checks():
    """WATCH_ONLY=news,youtube limits a run to those sources; unset means all."""
    wanted = [n.strip().lower() for n in os.environ.get("WATCH_ONLY", "").split(",") if n.strip()]
    unknown = [n for n in wanted if n not in CHECKS]
    if unknown:
        log(f"WATCH_ONLY has unknown sources {unknown}; valid: {', '.join(CHECKS)}")
    return [n for n in CHECKS if n in wanted] or list(CHECKS)


def main(now=None):
    now = now or datetime.now(IST)
    _cache.clear()
    if now.date() > STOP_AFTER:
        log(f"Past {STOP_AFTER}, watcher is retired. Disable the workflow.")
        return 0

    if os.environ.get("GITHUB_ACTIONS") and not (
        os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    ):
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are missing.")
        return 1

    state = load_state()
    names = enabled_checks()
    # Forget sources this runner no longer checks so they don't linger as red.
    prefixes = tuple(CHECKS[n][1] for n in names)
    state["health"] = {k: v for k, v in state["health"].items() if k.startswith(prefixes)}
    for film in FILMS:
        for name in names:
            try:
                globals()[CHECKS[name][0].__name__](state, film)
            except Exception as e:  # one source failing must not break the others
                log(f"{name} check crashed for {film['name']}: {type(e).__name__}: {e}")

    days_left = (date(2026, 12, 18) - now.date()).days
    # Announce on the first run, and again whenever the set of sources changes.
    if not state["started"] or state.get("sources") != names:
        state["sources"] = names
        state["started"] = send_telegram(
            f"✅ <b>Watcher is live ({html.escape(WATCH_NAME)})</b>\n"
            "Watching Kolkata bookings for Dune: Part Three and Avengers: Doomsday "
            f"({days_left} days to release).\n\n{status_lines(state)}"
        )
        if state["started"]:
            state["last_heartbeat"] = now.strftime("%Y-%m-%d")
    elif (
        now.weekday() == 6
        and now.hour >= 10
        and state["last_heartbeat"] != now.strftime("%Y-%m-%d")
    ):
        if send_telegram(
            f"💓 <b>Weekly heartbeat ({html.escape(WATCH_NAME)})</b> — still watching, "
            f"{days_left} days to release.\n\n"
            f"{status_lines(state)}"
        ):
            state["last_heartbeat"] = now.strftime("%Y-%m-%d")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
