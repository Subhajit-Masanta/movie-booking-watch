# Kolkata booking watcher

Sends a Telegram alert the moment advance booking opens in Kolkata for
**Dune: Part Three** and **Avengers: Doomsday** (both release 18 Dec 2026).
Runs free on GitHub Actions every ~10 minutes.

## What it checks

| Source | What it detects |
| --- | --- |
| District (district.in) Kolkata film pages | Real show dates and sessions, cinema names, screen format (IMAX at INOX South City) |
| INOX / PVR official site (inoxmovies.com) for Kolkata | The film and its IMAX version going on sale, and which formats the coming-soon list plans |
| BookMyShow Kolkata pages (via Playwright) | "I'm interested" turning into a live "Book tickets" button. Skipped silently if Cloudflare blocks GitHub's servers |
| Google News RSS | India-specific booking headlines from the last 3 days |
| YouTube: Warner Bros. India, Marvel India | Uploads whose title names the film plus tickets/booking wording |
| Reddit r/IMAXIndia | Posts naming the film plus booking wording (unverified, best effort) |

X and Instagram can't be read without a login, so turn on post notifications
there yourself for @WarnerBrosIndia, @Marvel_India, @PicturesPVR and @IMAX.

## Alerts

- 🎟️ **BOOKING OPEN** — showtimes are live
- 🎬 **IMAX CONFIRMED** — an IMAX session is listed in Kolkata
- 🗓️ **IMAX PLANNED** — INOX's coming-soon list shows IMAX for the film (booking not open yet)
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

## How it gets past the blocks

District, INOX and BookMyShow all reject requests from GitHub's servers (HTTP
403). For BookMyShow the watcher re-reads the page through Google Translate's
page relay, which fetches it from Google's side and returns it intact, including
the list of bookable formats (so IMAX in Kolkata is detected there). District
refuses even that route and INOX's API can't use it, so the GitHub workflow
leaves both out (`WATCH_ONLY`).

So on GitHub: BookMyShow, Google News, YouTube and Reddit. If BookMyShow stops
being readable you get a "Watcher is blind" alert within about an hour. Running
the script on a PC as below adds District and INOX on top.

## Google helper (recommended): runs every 5 minutes, adds INOX and District

GitHub's own schedule on free repos often runs hours late. `relay.gs` is a free
Google Apps Script that starts the workflow every 5 minutes, and relays INOX and
District requests (which block GitHub) from Google's side.

1. **GitHub token.** github.com → Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token. Expiration: 90 days.
   Repository access: only `movie-booking-watch`. Permissions → Actions:
   **Read and write**. Copy the token.
2. **Script.** https://script.google.com → New project. Replace the sample code
   with `relay.gs`.
3. **Settings.** Project Settings (gear) → Script Properties → add
   `GITHUB_TOKEN` (the token) and `RELAY_KEY` (any long random text).
4. **Schedule.** Back in the editor pick `setup` in the function list → Run →
   approve the permission prompt. It schedules a run every 5 minutes.
5. **Check the sites.** Pick `checkSites` → Run. The log says whether INOX and
   District work through Google.
6. **Relay (only if step 5 said WORKS).** Deploy → New deployment → gear → Web app,
   Execute as **Me**, access **Anyone** → Deploy. On GitHub add secrets `RELAY_URL`
   (the `/exec` URL) and `RELAY_KEY` (same text as in step 3).

To stop it, delete the trigger under Triggers (clock icon) in the script.

## Optional: also run on a PC (Windows)

```
pip install -r requirements-pc.txt
python -m playwright install chromium
copy .env.example .env
```

Put your bot token and chat ID in `.env`, then test with `python watcher.py`.
Schedule it every 10 minutes (runs hidden, also on battery):

```
schtasks /Create /TN "Movie booking watch" /SC MINUTE /MO 10 /F /TR "\"C:\Python314\pythonw.exe\" \"%CD%\watcher.py\""
```

Output goes to `watcher.log`. Remove it later with `schtasks /Delete /TN "Movie booking watch" /F`.

Without a token the script does a dry run and prints the messages instead of sending them.

Tests (offline, no network): `python -m unittest -v`

## Changing what is watched

Edit `FILMS` in `watcher.py`.
