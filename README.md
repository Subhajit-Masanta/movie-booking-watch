# Kolkata booking watcher

Sends a Telegram alert the moment advance booking opens in Kolkata for
**Dune: Part Three** and **Avengers: Doomsday** (both release 18 Dec 2026).
Runs free on GitHub Actions every ~10 minutes.

## What it checks

| Source | What it detects |
| --- | --- |
| District (district.in) Kolkata film pages | Real show dates and sessions, cinema names, screen format (IMAX at INOX South City) |
| PVR INOX official booking list for Kolkata | The film, and its IMAX version, appearing on pvrcinemas.com's bookable list |
| BookMyShow Kolkata pages (via Playwright) | "I'm interested" turning into a live "Book tickets" button. Skipped silently if Cloudflare blocks GitHub's servers |
| Google News RSS | India-specific booking headlines from the last 3 days |
| YouTube: Warner Bros. India, Marvel India | Uploads whose title names the film plus tickets/booking wording |
| Reddit r/IMAXIndia | Posts naming the film plus booking wording (unverified, best effort) |

X and Instagram can't be read without a login, so turn on post notifications
there yourself for @WarnerBrosIndia, @Marvel_India, @PicturesPVR and @IMAX.

## Alerts

- 🎟️ **BOOKING OPEN** — showtimes are live
- 🎬 **IMAX CONFIRMED** — an IMAX session is listed in Kolkata
- 📅 **BOOKING NEWS** — an India booking headline appeared
- 📣 **OFFICIAL VIDEO** / 💬 **REDDIT CHATTER** — a studio upload or community post about tickets
- ⚠️ **Watcher is blind** — a source failed for about an hour, so check manually
- ✅ **Watcher is live** on the first run, 💓 heartbeat every Sunday after 10:00 IST

Each alert is sent once; `state.json` remembers what was sent.
The watcher stops checking after 25 Dec 2026.

## Setup

1. **Create a Telegram bot.** Message `@BotFather`, send `/newbot`, copy the token.
2. **Get your chat ID.** Send your bot any message, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. **Create a public GitHub repo** (public repos get unlimited Actions minutes) and push this folder:
   ```
   git init -b main
   git add .
   git commit -m "Booking watcher"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
4. **Add secrets** under Settings → Secrets and variables → Actions:
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. **Test.** Actions tab → *Booking watch* → *Run workflow*. You should get
   "✅ Watcher is live" on Telegram within a minute.

Never put the token in the code; the repo is public.

## Run locally

```
pip install -r requirements.txt
python -m playwright install chromium
python watcher.py
```

Without the two environment variables it does a dry run and prints the messages instead of sending them.

Tests (offline, no network): `python -m unittest -v`

## Changing what is watched

Edit `FILMS` in `watcher.py`.
