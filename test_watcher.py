"""Offline tests for watcher.py. Run with: python -m unittest -v"""

import json
import tempfile
import time
import pathlib
import unittest
from datetime import datetime
from unittest import mock

import watcher as w

FILM = {
    "key": "dune3",
    "name": "Dune: Part Three",
    "title_words": ["dune"],
    "district": "https://example.test/dune-in-kolkata",
    "bms": "https://example.test/bms-dune",
    "news": ["Dune Part Three advance booking India"],
}


def district_page(name="Dune: Part Three", dates=(), cinemas=()):
    props = {
        "props": {
            "pageProps": {
                "data": {
                    "serverState": {
                        "meta": {"movie": {"name": name}, "showDates": list(dates)},
                        "pageData": {"nearbyCinemas": list(cinemas)},
                    }
                }
            }
        }
    }
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(props)
        + "</script></body></html>"
    )


def cinema(name, city="kolkata", sessions=()):
    return {"cinemaInfo": {"name": name, "cityKey": city}, "sessions": list(sessions)}


def session(when="2026-12-18T09:00", fmt="2D", audi="AUDI 1"):
    return {"showTime": when, "scrnFmt": fmt, "audi": audi, "premiumLabel": ""}


SOUTH_CITY = "INOX South City Mall, South City Mall, Kolkata"


def rss(*items):
    """items: (title, age_in_days)"""
    body = ""
    for i, (title, age) in enumerate(items):
        stamp = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - age * 86400)
        )
        body += (
            f"<item><title>{title}</title><link>https://example.test/n{i}</link>"
            f"<pubDate>{stamp}</pubDate></item>"
        )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def pvr(*names):
    films = [{"filmName": n, "format": "IMAX" if "IMAX" in n else ""} for n in names]
    return json.dumps({"output": {"mv": [{"films": films}]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.send_ok = True
        self.pages = {}
        w._cache.clear()
        self.state = {"alerted": {}, "health": {}, "last_heartbeat": None, "started": True}

        def fake_send(text, silent=False):
            if self.send_ok:
                self.sent.append(text)
            return self.send_ok

        def fake_fetch(url, post=None, headers=None):
            for prefix, page in self.pages.items():
                if url.startswith(prefix):
                    if isinstance(page, Exception):
                        raise page
                    return page
            raise RuntimeError(f"unexpected fetch {url}")

        for target, fake in (("send_telegram", fake_send), ("fetch", fake_fetch)):
            p = mock.patch.object(w, target, fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(w, "log", lambda msg: None)
        p.start()
        self.addCleanup(p.stop)

    def titles(self):
        return [t.splitlines()[0] for t in self.sent]


class DistrictTests(Base):
    def test_not_open_sends_nothing(self):
        self.pages[FILM["district"]] = district_page()
        w.check_district(self.state, FILM)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state["health"]["District / Dune: Part Three"]["status"], "ok")

    def test_booking_open_and_imax_alert_once(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"],
            cinemas=[cinema(SOUTH_CITY, sessions=[session(fmt="IMAX 2D", audi="IMAX 4")])],
        )
        for _ in range(3):
            w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("BOOKING OPEN", self.sent[0])
        self.assertIn("IMAX CONFIRMED", self.sent[1])
        self.assertNotIn("double-check", self.sent[1])

    def test_imax_arrives_later_than_booking(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"], cinemas=[cinema("PVR Mani Square", sessions=[session()])]
        )
        w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"],
            cinemas=[cinema(SOUTH_CITY, sessions=[session(fmt="IMAX 2D")])],
        )
        w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("IMAX CONFIRMED", self.sent[1])

    def test_imax_found_on_a_later_date_page(self):
        url = FILM["district"]
        self.pages[url + "?fromdate=2026-12-19"] = district_page(
            dates=["2026-12-18", "2026-12-19"],
            cinemas=[cinema(SOUTH_CITY, sessions=[session("2026-12-19T22:00", "IMAX 2D")])],
        )
        self.pages[url] = district_page(
            dates=["2026-12-18", "2026-12-19"],
            cinemas=[cinema("PVR Mani Square", sessions=[session()])],
        )
        w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("2026-12-19 22:00", self.sent[1])

    def test_sessions_only_in_another_city(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"],
            cinemas=[cinema("PVR Juhu, Mumbai", city="mumbai", sessions=[session(fmt="IMAX 2D")])],
        )
        w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("not Kolkata yet", self.sent[0])

    def test_imax_outside_south_city_is_flagged(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"],
            cinemas=[cinema("Some New IMAX, Kolkata", sessions=[session(fmt="IMAX 2D")])],
        )
        w.check_district(self.state, FILM)
        self.assertIn("double-check", self.sent[1])

    def test_wrong_film_page_is_a_failure_not_a_no(self):
        self.pages[FILM["district"]] = district_page(
            name="Some Other Film",
            dates=["2026-12-18"],
            cinemas=[cinema(SOUTH_CITY, sessions=[session(fmt="IMAX 2D")])],
        )
        w.check_district(self.state, FILM)
        self.assertEqual(self.sent, [])
        self.assertNotEqual(self.state["health"]["District / Dune: Part Three"]["status"], "ok")

    def test_blind_warning_once_then_recovery(self):
        self.pages[FILM["district"]] = w.Blocked("HTTP 403")
        for _ in range(w.BLIND_AFTER - 1):
            w.check_district(self.state, FILM)
        self.assertEqual(self.sent, [])
        for _ in range(5):
            w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("blind", self.sent[0])
        frozen = json.dumps(self.state, sort_keys=True)
        w.check_district(self.state, FILM)
        self.assertEqual(json.dumps(self.state, sort_keys=True), frozen)  # no commit churn

        self.pages[FILM["district"]] = district_page()
        w.check_district(self.state, FILM)
        self.assertIn("readable again", self.sent[1])

    def test_garbage_page_is_a_failure(self):
        self.pages[FILM["district"]] = "<html>Just a moment...</html>"
        w.check_district(self.state, FILM)
        self.assertEqual(self.state["health"]["District / Dune: Part Three"]["fails"], 1)

    def test_failed_send_is_retried_next_run(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"], cinemas=[cinema("PVR Mani Square", sessions=[session()])]
        )
        self.send_ok = False
        w.check_district(self.state, FILM)
        self.assertEqual(self.state["alerted"], {})
        self.send_ok = True
        w.check_district(self.state, FILM)
        self.assertEqual(len(self.sent), 1)

    def test_special_characters_are_escaped(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"],
            cinemas=[cinema("A&B <Gold> Cinema, Kolkata", sessions=[session()])],
        )
        w.check_district(self.state, FILM)
        self.assertIn("A&amp;B &lt;Gold&gt; Cinema", self.sent[0])


class PvrTests(Base):
    def test_not_listed(self):
        self.pages[w.PVR_URL] = pvr("THE ODYSSEY (ENGLISH IMAX)")
        w.check_pvr(self.state, FILM)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state["health"]["PVR INOX Kolkata"]["status"], "ok")

    def test_listed_without_imax(self):
        self.pages[w.PVR_URL] = pvr("DUNE: PART THREE (ENGLISH)")
        w.check_pvr(self.state, FILM)
        w.check_pvr(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ON SALE AT INOX", self.sent[0])

    def test_listed_with_imax(self):
        self.pages[w.PVR_URL] = pvr("DUNE: PART THREE (ENGLISH)", "DUNE: PART THREE (ENGLISH IMAX)")
        w.check_pvr(self.state, FILM)
        w.check_pvr(self.state, FILM)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("IMAX CONFIRMED", self.sent[1])

    def soon(self, *formats, name="DUNE: PART THREE (ENGLISH)"):
        return json.dumps(
            {"output": {"movies": [{"filmName": name, "n": name, "secondaryFormats": list(formats)}]}}
        )

    def test_inox_host_is_tried_first_then_pvr(self):
        self.pages[w.INOX_URL] = pvr("DUNE: PART THREE (ENGLISH IMAX)")
        self.pages[w.PVR_URL] = RuntimeError("should not be needed")
        w.check_pvr(self.state, FILM)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("ON SALE AT INOX", self.sent[0])

    def test_planned_imax_alerts_once_and_says_not_open(self):
        self.pages[w.INOX_URL] = pvr("THE ODYSSEY (ENGLISH IMAX)")
        self.pages[w.INOX_SOON_URL] = self.soon("4dx", "ice", "imax")
        w.check_pvr(self.state, FILM)
        w.check_pvr(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("IMAX PLANNED", self.sent[0])
        self.assertIn("not open yet", self.sent[0])

    def test_planned_formats_without_imax_or_for_other_film_are_silent(self):
        self.pages[w.INOX_URL] = pvr("THE ODYSSEY (ENGLISH IMAX)")
        self.pages[w.INOX_SOON_URL] = self.soon("screen-x", "3d", "4dx")
        w.check_pvr(self.state, FILM)
        self.pages[w.INOX_SOON_URL] = self.soon("imax", name="AVENGERS: DOOMSDAY (ENGLISH)")
        w._cache.clear()
        w.check_pvr(self.state, FILM)
        self.assertEqual(self.sent, [])

    def test_coming_soon_failure_does_not_fail_the_source(self):
        self.pages[w.INOX_URL] = pvr("DUNE: PART THREE (ENGLISH)")
        self.pages[w.INOX_SOON_URL] = w.Blocked("HTTP 403")
        w.check_pvr(self.state, FILM)
        self.assertEqual(self.state["health"]["PVR INOX Kolkata"]["status"], "ok")
        self.assertEqual(len(self.sent), 1)

    def test_empty_or_broken_response_is_a_failure(self):
        for body in ('{"output": {"mv": []}}', '{"status": 403}', "not json"):
            self.pages[w.PVR_URL] = body
            w.check_pvr(self.state, FILM)
            self.assertNotEqual(self.state["health"]["PVR INOX Kolkata"]["status"], "ok")
        self.assertEqual(self.sent, [])


class BmsTests(Base):
    UPCOMING = {
        "title": "Dune: Part Three (2026) - Movie | Release Date in Kolkata - BookMyShow",
        "buttons": ["Sign in", "Contact today!"],
        "text": "Releasing on 18 Dec, 2026 Dune: Part Three Mark interested to know "
        "when bookings open I'm interested",
    }
    LIVE = {
        "title": "Dune: Part Three (2026) - Movie | Release Date in Kolkata - BookMyShow",
        "buttons": ["Sign in", "Book tickets", "Book tickets"],
        "text": "In cinemas Dune: Part Three Book tickets",
        "formats": ["2D", "4DX"],
    }

    def run_bms(self, snap, times=1):
        effect = snap if isinstance(snap, Exception) else None
        with mock.patch.object(
            w, "bms_snapshot", side_effect=effect, return_value=None if effect else snap
        ):
            for _ in range(times):
                w.check_bms(self.state, FILM)
        return self.state["health"].get("BookMyShow / Dune: Part Three", {}).get("status")

    def test_upcoming_page_is_ok_and_silent(self):
        self.assertEqual(self.run_bms(self.UPCOMING, times=3), "ok")
        self.assertEqual(self.sent, [])

    def test_book_button_alerts_once(self):
        self.assertEqual(self.run_bms(self.LIVE, times=3), "ok")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("BOOKING OPEN", self.sent[0])

    def test_imax_format_alerts_separately_even_if_it_arrives_later(self):
        self.run_bms(self.LIVE, times=2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Formats: 2D, 4DX", self.sent[0])
        self.run_bms(dict(self.LIVE, formats=["2D", "4DX", "IMAX 2D"]), times=2)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("IMAX CONFIRMED", self.sent[1])

    def test_imax_format_while_still_upcoming_does_not_alert(self):
        self.run_bms(dict(self.UPCOMING, formats=["IMAX 2D"]))
        self.assertEqual(self.sent, [])

    def test_formats_are_read_from_page_data(self):
        page = '{"formats":[{"dimension":"IMAX 2D"},{"dimension" : "3D SCREEN X"}],"x":{"dimension":"Select all"}}'
        self.assertEqual(w.bms_formats(page), ["3D SCREEN X", "IMAX 2D"])

    def test_block_warns_once_after_an_hour(self):
        blocked = {"title": "Attention Required! | Cloudflare", "buttons": [], "text": "blocked"}
        self.assertNotEqual(self.run_bms(blocked, times=w.BLIND_AFTER - 1), "ok")
        self.assertEqual(self.sent, [])
        self.run_bms(w.Blocked("HTTP 403"), times=20)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("blind", self.sent[0])

    def test_text_mentioning_booking_does_not_alert(self):
        snap = dict(self.UPCOMING, text=self.UPCOMING["text"] + " Book tickets on the app")
        self.run_bms(snap)
        self.assertEqual(self.sent, [])

    def test_other_film_with_book_button_does_not_alert(self):
        snap = dict(self.LIVE, title="Avengers Endgame: Encore (2026) - BookMyShow")
        self.assertEqual(self.run_bms(snap), "page is not this film")
        self.assertEqual(self.sent, [])

    def test_unknown_layout_is_not_read_as_not_open(self):
        snap = {"title": self.LIVE["title"], "buttons": ["Sign in"], "text": "Dune: Part Three"}
        self.assertEqual(self.run_bms(snap), "unrecognised page layout")

    def test_browser_crash_is_contained(self):
        self.assertIn("RuntimeError", self.run_bms(RuntimeError("chromium died")))
        self.assertEqual(self.sent, [])

    def test_no_url_configured(self):
        w.check_bms(self.state, dict(FILM, bms=None))
        self.assertEqual(self.state["health"], {})


class NewsTests(Base):
    PREFIX = "https://news.google.com/rss/search"

    def run_news(self, *items):
        self.pages[self.PREFIX] = rss(*items)
        w.check_news(self.state, FILM)

    def test_india_booking_headline_alerts_once(self):
        items = [("Dune Part Three advance booking opens in India on Nov 20 - Sacnilk", 0)]
        self.run_news(*items)
        self.run_news(*items)
        self.assertEqual(len(self.sent), 1)

    def test_same_story_from_two_outlets_is_one_alert(self):
        self.run_news(
            ("Dune: Part Three advance booking opens in India - Koimoi", 0),
            ("Dune Part Three advance booking opens in India - Sacnilk", 0),
        )
        self.assertEqual(len(self.sent), 1)

    def test_irrelevant_headlines_are_ignored(self):
        self.run_news(
            ("Dune Part Three IMAX tickets sell out in US presale - Variety", 0),
            ("Avengers: Doomsday advance booking opens in India - Koimoi", 0),
            ("Dune Part Three India advance booking date revealed - Old", 5),
            ("Dune Part Three trailer breaks records in India - NDTV", 0),
            ("Avengers: Endgame Encore (IMAX 2D) Advance Booking Report India", 0),
        )
        self.assertEqual(self.sent, [])

    def test_flood_is_capped_per_run(self):
        self.run_news(*[(f"Dune advance booking India update number {i} - X", 0) for i in range(9)])
        self.assertEqual(len(self.sent), w.NEWS_MAX_PER_RUN)

    def test_feed_down_marks_failure(self):
        self.pages[self.PREFIX] = RuntimeError("HTTP 500")
        w.check_news(self.state, FILM)
        self.assertEqual(self.state["health"]["Google News / Dune: Part Three"]["fails"], 1)


class SocialTests(Base):
    YT = "https://www.youtube.com/feeds/videos.xml"

    def test_youtube_ticket_video_alerts_once(self):
        self.pages[self.YT] = rss(
            ("Dune: Part Three | Tickets On Sale Now", 0),
            ("Dune: Part Three | Official Trailer", 0),
            ("Clayface | Book Now", 0),
            ("Dune: Part Three | Advance Bookings Open", 9),
        )
        with mock.patch.object(w, "YOUTUBE_CHANNELS", {"Warner Bros. India": "X"}):
            w.check_youtube(self.state, FILM)
            w.check_youtube(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Tickets On Sale Now", self.sent[0])

    def test_youtube_failure_is_silent(self):
        self.pages[self.YT] = RuntimeError("HTTP 500")
        for _ in range(10):
            w.check_youtube(self.state, FILM)
        self.assertEqual(self.sent, [])

    def test_reddit_post_and_block(self):
        self.pages[w.REDDIT_FEED] = rss(
            ("Dune 3 bookings are live at South City!", 0),
            ("Dune rewatch thoughts", 0),
        )
        w.check_reddit(self.state, FILM)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("unverified", self.sent[0])
        self.pages[w.REDDIT_FEED] = w.Blocked("HTTP 429")
        for _ in range(10):
            w.check_reddit(self.state, FILM)
        self.assertEqual(len(self.sent), 1)


class MainTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = pathlib.Path(self.tmp.name) / "state.json"
        env = mock.patch.dict("os.environ")  # a local .env must not narrow these runs
        env.start()
        self.addCleanup(env.stop)
        for name in ("WATCH_ONLY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SEND_STATUS", "RELAY_URL"):
            w.os.environ.pop(name, None)
        for target, value in (
            ("STATE_FILE", self.state_file),
            ("FILMS", [FILM]),
            ("bms_snapshot", mock.Mock(return_value=BmsTests.UPCOMING)),
        ):
            p = mock.patch.object(w, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.pages = {
            FILM["district"]: district_page(),
            w.PVR_URL: pvr("THE ODYSSEY (ENGLISH IMAX)"),
            "https://news.google.com/": rss(),
            "https://www.youtube.com/": rss(),
            w.REDDIT_FEED: rss(),
        }

    def at(self, y, m, d, hh=12):
        return datetime(y, m, d, hh, 0, tzinfo=w.IST)

    def test_first_run_sends_live_message_once(self):
        self.assertEqual(w.main(self.at(2026, 10, 12)), 0)
        self.assertEqual(w.main(self.at(2026, 10, 12, 13)), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Watcher is live", self.sent[0])
        self.assertIn("🟢 BookMyShow", self.sent[0])
        self.assertIn("🟢 PVR INOX Kolkata", self.sent[0])

    def test_watch_only_limits_sources_and_drops_stale_health(self):
        w.main(self.at(2026, 10, 12))
        with mock.patch.dict("os.environ", {"WATCH_ONLY": "news, youtube,reddit"}):
            self.pages[FILM["district"]] = RuntimeError("must not be fetched")
            w.main(self.at(2026, 10, 12, 13))
        health = json.loads(self.state_file.read_text(encoding="utf-8"))["health"]
        self.assertEqual(
            sorted(k.split(" / ")[0] for k in health), ["Google News", "Reddit", "YouTube", "YouTube"]
        )
        self.assertTrue(all(v["status"] == "ok" for v in health.values()))

    def test_live_message_repeats_when_sources_change(self):
        w.main(self.at(2026, 10, 12))
        with mock.patch.dict("os.environ", {"WATCH_ONLY": "district,news"}):
            w.main(self.at(2026, 10, 12, 13))
            w.main(self.at(2026, 10, 12, 14))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("PVR INOX", self.sent[1])

    def test_watch_only_typo_falls_back_to_everything(self):
        with mock.patch.dict("os.environ", {"WATCH_ONLY": "distric"}):
            self.assertEqual(w.enabled_checks(), list(w.CHECKS))

    def test_quiet_runs_do_not_change_state_file(self):
        w.main(self.at(2026, 10, 12))
        w.main(self.at(2026, 10, 12, 13))
        before = self.state_file.read_text(encoding="utf-8")
        w.main(self.at(2026, 10, 12, 14))  # same 12:00-15:00 heartbeat window, nothing new
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), before)

    def test_five_heartbeats_a_day_once_each(self):
        w.main(self.at(2026, 10, 17, 8))  # live message, counts for the 21:00 slot of the 16th
        w.main(self.at(2026, 10, 17, 8))
        self.assertEqual(len(self.sent), 1)
        for hour in (9, 10, 11, 12, 12, 13, 15, 18, 20, 21, 23):
            w.main(self.at(2026, 10, 17, hour))
        self.assertEqual(len(self.sent), 6)  # 9, 12, 15, 18, 21
        self.assertTrue(all("Status" in t for t in self.sent[1:]))
        self.assertIn("all 7 sources working", self.sent[1])
        self.assertIn("not open yet", self.sent[1])
        w.main(self.at(2026, 10, 18, 2))  # after midnight: the 21:00 one was already sent
        self.assertEqual(len(self.sent), 6)
        w.main(self.at(2026, 10, 18, 16))  # runs were stuck for hours: one message, not three
        self.assertEqual(len(self.sent), 7)

    def test_heartbeat_is_silent_and_reports_failures(self):
        calls = []
        with mock.patch.object(w, "send_telegram", lambda text, silent=False: (calls.append((text, silent)), True)[1]):
            w.main(self.at(2026, 10, 17, 8))
            self.pages[w.REDDIT_FEED] = RuntimeError("down")
            w.main(self.at(2026, 10, 17, 9))
        self.assertFalse(calls[0][1])  # the live message keeps its sound
        text, silent = calls[-1]
        self.assertTrue(silent)
        self.assertIn("1 of 7 sources failing", text)
        self.assertIn("🔴 Reddit", text)

    def test_manual_status_request_always_sends(self):
        w.main(self.at(2026, 10, 17, 15))
        w.main(self.at(2026, 10, 17, 15))
        self.assertEqual(len(self.sent), 1)
        with mock.patch.dict("os.environ", {"SEND_STATUS": "true"}):
            w.main(self.at(2026, 10, 17, 16))
            w.main(self.at(2026, 10, 17, 16))
        self.assertEqual(len(self.sent), 3)
        self.assertIn("Status 4 PM", self.sent[-1])
        with mock.patch.dict("os.environ", {"SEND_STATUS": "false"}):
            w.main(self.at(2026, 10, 17, 17))
        self.assertEqual(len(self.sent), 3)  # unticked box changes nothing

    def test_heartbeat_mentions_open_booking(self):
        w.main(self.at(2026, 10, 17, 8))
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["alerted"]["dune3:bms:booking_open"] = "2026-10-17 08:10"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        w.main(self.at(2026, 10, 17, 9))
        self.assertIn("Booking open: Dune: Part Three", self.sent[-1])

    def test_stops_after_cutoff(self):
        w.main(self.at(2026, 12, 25))
        self.assertEqual(len(self.sent), 1)
        self.pages = {}
        self.assertEqual(w.main(self.at(2026, 12, 26)), 0)
        self.assertEqual(len(self.sent), 1)

    def test_one_crashing_source_does_not_stop_the_rest(self):
        self.pages[FILM["district"]] = district_page(
            dates=["2026-12-18"], cinemas=[cinema("PVR Mani Square", sessions=[session()])]
        )
        def check_pvr(state, film):
            raise KeyError("boom")

        with mock.patch.object(w, "check_pvr", check_pvr):
            self.assertEqual(w.main(self.at(2026, 10, 12)), 0)
        self.assertTrue(any("BOOKING OPEN" in t for t in self.sent))

    def test_corrupt_state_file_recovers(self):
        self.state_file.write_text("{broken", encoding="utf-8")
        self.assertEqual(w.main(self.at(2026, 10, 12)), 0)
        json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_missing_secrets_fail_loudly_on_actions(self):
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=True):
            self.assertEqual(w.main(self.at(2026, 10, 12)), 1)


class FetchTests(unittest.TestCase):
    def setUp(self):
        w._cache.clear()
        self.calls = []
        self.responses = {}

        def fake_get(url, headers=None, timeout=None):
            self.calls.append(url)
            for part, (status, text) in self.responses.items():
                if part in url:
                    return mock.Mock(status_code=status, text=text)
            raise AssertionError(f"unexpected url {url}")

        for target, fake in (("get", fake_get), ("post", mock.Mock(return_value=mock.Mock(status_code=403)))):
            p = mock.patch.object(w.requests, target, fake)
            p.start()
            self.addCleanup(p.stop)
        for target in ("log", "time"):
            p = mock.patch.object(w, target, mock.Mock(time=mock.Mock(return_value=600), sleep=mock.Mock()))
            p.start()
            self.addCleanup(p.stop)

    def test_relay_url_shape(self):
        self.assertEqual(
            w.relay_url("https://www.district.in/movies/x-MV1?fromdate=2026-12-18"),
            "https://www-district-in.translate.goog/movies/x-MV1"
            "?fromdate=2026-12-18&nc=10&_x_tr_sl=hi&_x_tr_tl=en&_x_tr_hl=en",
        )
        self.assertTrue(
            w.relay_url("https://in.bookmyshow.com/movies/a/ET1").startswith(
                "https://in-bookmyshow-com.translate.goog/movies/a/ET1?nc="
            )
        )

    def test_direct_success_does_not_use_relay(self):
        self.responses = {"www.district.in": (200, "direct")}
        self.assertEqual(w.fetch("https://www.district.in/movies/x"), "direct")
        self.assertEqual(len(self.calls), 1)

    def test_blocked_site_falls_back_to_relay_and_caches(self):
        self.responses = {"www.district.in": (403, ""), "translate.goog": (200, "relayed")}
        self.assertEqual(w.fetch("https://www.district.in/movies/x"), "relayed")
        self.assertEqual(w.fetch("https://www.district.in/movies/x"), "relayed")
        self.assertEqual(len(self.calls), 2)

    def test_relay_also_blocked_raises(self):
        self.responses = {"www.district.in": (403, ""), "translate.goog": (429, "")}
        with self.assertRaises(w.Blocked):
            w.fetch("https://www.district.in/movies/x")

    def own_relay_reply(self, status, body="", http=200, broken=False):
        reply = mock.Mock(status_code=http, text="<html><b>Script error</b> try later</html>")
        reply.json = mock.Mock(
            side_effect=ValueError("no json") if broken else None,
            return_value={"status": status, "body": body},
        )
        return mock.patch.object(w.requests, "post", mock.Mock(return_value=reply))

    def test_own_relay_carries_blocked_post_with_headers(self):
        env = {"RELAY_URL": "https://script.test/exec", "RELAY_KEY": "k"}
        with mock.patch.dict("os.environ", env), self.own_relay_reply(200, "inox-json") as post:
            # first call is the direct POST (403), second goes to the relay
            post.side_effect = [mock.Mock(status_code=403), post.return_value]
            text = w.fetch(w.INOX_URL, post={"city": "Kolkata"}, headers=w.chain_headers(w.INOX_URL))
        self.assertEqual(text, "inox-json")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "https://script.test/exec")
        self.assertEqual((sent["key"], sent["method"], sent["url"]), ("k", "post", w.INOX_URL))
        self.assertEqual(sent["headers"]["city"], "Kolkata")
        self.assertEqual(json.loads(sent["body"]), {"city": "Kolkata"})
        self.assertNotIn("Content-Type", sent["headers"])

    def test_own_relay_is_second_choice_for_district(self):
        self.responses = {"www.district.in": (403, ""), "translate.goog": (400, "")}
        env = {"RELAY_URL": "https://script.test/exec"}
        with mock.patch.dict("os.environ", env), self.own_relay_reply(200, "page"):
            self.assertEqual(w.fetch("https://www.district.in/movies/x"), "page")
        self.assertEqual(sum("translate.goog" in c for c in self.calls), 2)  # tried, with retry

    def test_own_relay_failures_surface(self):
        self.responses = {"www.district.in": (403, ""), "translate.goog": (400, "")}
        env = {"RELAY_URL": "https://script.test/exec"}
        with mock.patch.dict("os.environ", env):
            with self.own_relay_reply(403), self.assertRaises(w.Blocked):
                w.fetch("https://www.district.in/movies/a")
            with self.own_relay_reply(401), self.assertRaises(w.Blocked):
                w.fetch("https://www.district.in/movies/b")
            with self.own_relay_reply(0, broken=True, http=500) as post, self.assertRaises(RuntimeError):
                w.fetch("https://www.district.in/movies/c")
            self.assertEqual(post.call_count, 2)  # retried once
            self.assertIn("Script error try later", str(w.log.call_args_list))

    def test_own_relay_recovers_on_retry(self):
        self.responses = {"www.district.in": (403, ""), "translate.goog": (400, "")}
        bad = mock.Mock(status_code=404, text="<html>Not Found</html>", json=mock.Mock(side_effect=ValueError))
        good = mock.Mock(status_code=200, json=mock.Mock(return_value={"status": 200, "body": "page"}))
        with mock.patch.dict("os.environ", {"RELAY_URL": "https://script.test/exec"}):
            with mock.patch.object(w.requests, "post", mock.Mock(side_effect=[bad, good])):
                self.assertEqual(w.fetch("https://www.district.in/movies/d"), "page")

    def test_other_sites_and_posts_never_use_relay(self):
        self.responses = {"news.google.com": (403, "")}
        with self.assertRaises(w.Blocked):
            w.fetch("https://news.google.com/rss")
        with self.assertRaises(w.Blocked):
            w.fetch(w.PVR_URL, post={"city": "Kolkata"})
        self.assertFalse(any("translate.goog" in c for c in self.calls))


class BotMessageTests(Base):
    NOW = datetime(2026, 10, 17, 15, 47, tzinfo=w.IST)

    def setUp(self):
        super().setUp()
        self.state["health"] = {"BookMyShow / Dune: Part Three": {"status": "ok"}}
        env = mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "111"})
        env.start()
        self.addCleanup(env.stop)

    def run_bot(self, *messages, chat=111, age=60, ok=True):
        updates = [
            {"update_id": 500 + i, "message": {"chat": {"id": chat}, "text": text,
                                               "date": int(self.NOW.timestamp()) - age}}
            for i, text in enumerate(messages)
        ]
        reply = mock.Mock(ok=ok, json=mock.Mock(return_value={"ok": True, "result": updates}))
        with mock.patch.object(w.requests, "get", mock.Mock(return_value=reply)) as get:
            w.answer_messages(self.state, self.NOW, 62)
        return get

    def test_status_request_gets_one_reply(self):
        self.run_bot("status", "Is it working?", "status")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Status 3:47 PM", self.sent[0])
        self.assertIn("all 1 sources working", self.sent[0])
        self.assertEqual(self.state["telegram_offset"], 502)

    def test_next_run_only_reads_new_messages(self):
        self.run_bot("status")
        get = self.run_bot()
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 501)
        self.assertEqual(len(self.sent), 1)

    def test_other_text_gets_help(self):
        self.run_bot("hello")
        self.assertIn("Send <b>status</b>", self.sent[0])

    def test_strangers_are_ignored(self):
        self.run_bot("status", chat=999)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state["telegram_offset"], 500)

    def test_old_history_ignored_on_first_run_only(self):
        self.run_bot("hi", "How", age=3 * 86400)
        self.assertEqual(self.sent, [])
        self.run_bot("status", age=3 * 86400)  # later runs answer even if a run was delayed
        self.assertEqual(len(self.sent), 1)

    def test_telegram_down_is_harmless(self):
        with mock.patch.object(w.requests, "get", side_effect=w.requests.ConnectionError("x")):
            w.answer_messages(self.state, self.NOW, 62)
        self.assertEqual(self.sent, [])
        self.run_bot("status", ok=False)
        self.assertEqual(self.sent, [])


class TelegramTests(unittest.TestCase):
    def test_send_reports_failure_and_success(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict("os.environ", env), mock.patch.object(w, "log"):
            with mock.patch.object(w.requests, "post") as post:
                post.return_value = mock.Mock(ok=False, status_code=400, text="bad")
                self.assertFalse(w.send_telegram("x"))
                post.return_value = mock.Mock(ok=True)
                self.assertTrue(w.send_telegram("x"))
                self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")
                self.assertFalse(post.call_args.kwargs["json"]["disable_notification"])
                w.send_telegram("x", silent=True)
                self.assertTrue(post.call_args.kwargs["json"]["disable_notification"])
                post.side_effect = w.requests.ConnectionError("down")
                self.assertFalse(w.send_telegram("x"))

    def test_token_never_logged_on_network_error(self):
        env = {"TELEGRAM_BOT_TOKEN": "SECRET123", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict("os.environ", env), mock.patch.object(w, "log") as log:
            with mock.patch.object(
                w.requests, "post", side_effect=w.requests.ConnectionError("url had SECRET123")
            ):
                w.send_telegram("x")
        self.assertNotIn("SECRET123", str(log.call_args_list))


if __name__ == "__main__":
    unittest.main()
