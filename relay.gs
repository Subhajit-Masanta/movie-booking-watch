/**
 * Google Apps Script helper for the booking watcher. Runs free on Google's side.
 *
 * It does three jobs:
 *  1. Every 5 minutes it fetches the District pages the watcher asked for and
 *     keeps a fresh copy, then starts the GitHub "Booking watch" run. GitHub's
 *     own schedule is often hours late on free repos.
 *  2. Hands those copies to the watcher on request. District refuses pages
 *     requested on GitHub's behalf, but answers this script's own timer.
 *  3. Answers "status" messages you send the Telegram bot within seconds,
 *     once connectTelegram() has been run.
 *
 * Settings live in Project Settings -> Script Properties, never in this code:
 *   GITHUB_TOKEN  fine-grained token with "Actions: Read and write" on the repo
 *   RELAY_KEY     any long random text; the same text goes in GitHub's RELAY_KEY secret
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   same values as the GitHub secrets (job 3 only)
 *
 * After changing this code: Deploy -> Manage deployments -> pencil ->
 * Version: New version -> Deploy. The web app URL stays the same.
 */

const REPO = "Subhajit-Masanta/movie-booking-watch";
const WORKFLOW = "watch.yml";
const ALLOWED = /(^|\.)(inoxmovies\.com|pvrcinemas\.com|district\.in|bookmyshow\.com)$/;
const COPY_SECONDS = 600; // a fetched copy is served for up to 10 minutes
const FORGET_MS = 3600 * 1000; // stop refreshing pages nobody asked for in an hour
const MAX_PAGES = 10; // keeps the timer well inside the free daily run-time quota

function prop(name) {
  return PropertiesService.getScriptProperties().getProperty(name) || "";
}

// ---------------------------------------------------------------- timer

/** Run once from the editor: replaces any old schedule with one every 5 minutes. */
function setup() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === "startWatch")
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger("startWatch").timeBased().everyMinutes(5).create();
  startWatch();
  Logger.log("Scheduled every 5 minutes, and started one run now.");
}

/** Timer job: refresh the page copies, then ask GitHub to start the watcher. */
function startWatch() {
  if (new Date() > new Date("2026-12-26T00:00:00+05:30")) return; // films are out
  try {
    refreshCopies();
  } catch (err) {
    Logger.log("Refreshing copies failed: " + err); // still start the watcher
  }
  const res = UrlFetchApp.fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "post",
      contentType: "application/json",
      headers: {
        Authorization: "Bearer " + prop("GITHUB_TOKEN"),
        Accept: "application/vnd.github+json",
      },
      payload: JSON.stringify({ ref: "main" }),
      muteHttpExceptions: true,
    }
  );
  if (res.getResponseCode() !== 204) {
    throw new Error(`GitHub said ${res.getResponseCode()}: ${res.getContentText().slice(0, 300)}`);
  }
}

// ---------------------------------------------------------------- page copies

function wanted() {
  try {
    return JSON.parse(prop("WANTED") || "{}");
  } catch (err) {
    return {};
  }
}

function remember(url) {
  const list = wanted();
  list[url] = Date.now();
  const newest = Object.keys(list)
    .sort((a, b) => list[b] - list[a])
    .slice(0, MAX_PAGES);
  const kept = {};
  newest.forEach((u) => (kept[u] = list[u]));
  PropertiesService.getScriptProperties().setProperty("WANTED", JSON.stringify(kept));
}

function copyKey(url) {
  return "p:" + Utilities.base64EncodeWebSafe(
    Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, url, Utilities.Charset.UTF_8)
  );
}

// The cache holds at most 100 KB per item, and a District page during sales is
// close to 1 MB, so copies are compressed and split into pieces.
const PIECE = 90000;

function saveCopy(url, text) {
  const packed = Utilities.base64Encode(Utilities.gzip(Utilities.newBlob(text, "text/html")).getBytes());
  const key = copyKey(url);
  const items = {};
  let count = 0;
  for (let i = 0; i < packed.length; i += PIECE) items[`${key}:${count++}`] = packed.slice(i, i + PIECE);
  items[key] = String(count); // written last in the same call, so readers never see half a copy
  CacheService.getScriptCache().putAll(items, COPY_SECONDS);
}

function loadCopy(url) {
  const cache = CacheService.getScriptCache();
  const key = copyKey(url);
  const count = Number(cache.get(key) || 0);
  if (!count) return null;
  const names = [];
  for (let i = 0; i < count; i++) names.push(`${key}:${i}`);
  const pieces = cache.getAll(names);
  if (names.some((n) => !pieces[n])) return null; // a piece expired: treat as no copy
  const blob = Utilities.newBlob(Utilities.base64Decode(names.map((n) => pieces[n]).join("")), "application/x-gzip");
  return Utilities.ungzip(blob).getDataAsString("UTF-8");
}

/** Fetches every page the watcher asked for recently and stores a fresh copy. */
function refreshCopies() {
  const list = wanted();
  const now = Date.now();
  Object.keys(list).forEach((url) => {
    if (now - list[url] > FORGET_MS) return;
    const res = UrlFetchApp.fetch(url, { muteHttpExceptions: true, followRedirects: true });
    if (res.getResponseCode() === 200) saveCopy(url, res.getContentText());
    Logger.log(`${res.getResponseCode()} ${url}`);
  });
}

// ---------------------------------------------------------------- web app

/** Run once from the editor to see whether INOX and District answer Google. */
function checkSites() {
  const tests = [
    ["District", "https://www.district.in/movies/dune-part-three-movie-tickets-in-kolkata-MV207332", "get", null, "__NEXT_DATA__"],
    ["INOX", "https://api3.inoxmovies.com/api/v1/booking/content/nowshowing", "post", '{"city":"Kolkata"}', "filmName"],
  ];
  const headers = {
    city: "Kolkata", appVersion: "1.0", platform: "WEBSITE", country: "INDIA", chain: "INOX",
    Origin: "https://www.inoxmovies.com", Referer: "https://www.inoxmovies.com/",
  };
  tests.forEach(([name, url, method, body, marker]) => {
    const options = { method: method, headers: method === "post" ? headers : {}, muteHttpExceptions: true };
    if (body) { options.payload = body; options.contentType = "application/json"; }
    const res = UrlFetchApp.fetch(url, options);
    const ok = res.getResponseCode() === 200 && res.getContentText().indexOf(marker) >= 0;
    Logger.log(`${name}: HTTP ${res.getResponseCode()} -> ${ok ? "WORKS through Google" : "blocked"}`);
  });
}

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply(400, "bad request");
  }
  if (req.update_id !== undefined) return telegramUpdate(req);
  if (!prop("RELAY_KEY") || String(req.key || "").trim() !== prop("RELAY_KEY").trim()) {
    return reply(401, "wrong key");
  }

  const url = String(req.url || "");
  const host = url.replace(/^https:\/\//, "").split(/[/?#]/)[0];
  if (!url.startsWith("https://") || !ALLOWED.test(host)) return reply(400, "host not allowed");

  const isGet = req.method !== "post";
  if (isGet) {
    remember(url); // the timer keeps this page fresh from now on
    const copy = loadCopy(url);
    if (copy !== null) return reply(200, copy);
  }

  // No fresh copy yet: try live. District usually refuses this one, but the
  // timer will have a copy ready for the next run.
  const options = {
    method: isGet ? "get" : "post",
    headers: req.headers || {},
    muteHttpExceptions: true,
    followRedirects: true,
  };
  if (req.body) {
    options.payload = req.body;
    options.contentType = "application/json";
  }
  try {
    const res = UrlFetchApp.fetch(url, options);
    if (isGet && res.getResponseCode() === 200) saveCopy(url, res.getContentText());
    return reply(res.getResponseCode(), res.getContentText());
  } catch (err) {
    return reply(502, String(err));
  }
}

// ---------------------------------------------------------------- telegram replies

const WEB_APP_URL =
  "https://script.google.com/macros/s/AKfycbyfs9bNqlQd8oX9GZkEu_7cYxRcFNjuHkU5uvSZPj7flZmVzPb9hwd7mz3kscaYKGUb/exec";
const STATUS_WORDS = ["status", "working", "alive", "check", "update", "/start"];
const FILM_NAMES = { dune3: "Dune: Part Three", doomsday: "Avengers: Doomsday" };
const HELP_TEXT =
  'Send <b>status</b> (or "working?") and I reply with the status of every source. ' +
  "Booking alerts arrive on their own.";

/** Run once from the editor: Telegram then delivers your messages here instantly. */
function connectTelegram() {
  const res = telegramApi("setWebhook", {
    url: WEB_APP_URL,
    allowed_updates: ["message"],
    drop_pending_updates: true,
  });
  Logger.log(res.getContentText());
}

/** Run to undo connectTelegram(): replies go back to the ~5 minute GitHub runs. */
function disconnectTelegram() {
  Logger.log(telegramApi("deleteWebhook", {}).getContentText());
}

function telegramApi(method, body) {
  return UrlFetchApp.fetch(`https://api.telegram.org/bot${prop("TELEGRAM_BOT_TOKEN")}/${method}`, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify(body),
    muteHttpExceptions: true,
  });
}

function telegramUpdate(update) {
  // Telegram may deliver the same update again; answer each one only once.
  const cache = CacheService.getScriptCache();
  const seen = "tg:" + update.update_id;
  if (cache.get(seen)) return reply(200, "already answered");
  cache.put(seen, "1", 21600);

  const msg = update.message || {};
  if (String((msg.chat || {}).id) !== prop("TELEGRAM_CHAT_ID").trim()) {
    return reply(200, "ignored"); // only answer you, never strangers who find the bot
  }
  const text = String(msg.text || "").toLowerCase();
  if (!text) return reply(200, "no text");
  const asked = STATUS_WORDS.some((w) => text.indexOf(w) >= 0);
  telegramApi("sendMessage", {
    chat_id: prop("TELEGRAM_CHAT_ID").trim(),
    text: asked ? statusText(new Date()) : HELP_TEXT,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });
  return reply(200, "answered");
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function github(path, accept) {
  const res = UrlFetchApp.fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    headers: { Authorization: "Bearer " + prop("GITHUB_TOKEN"), Accept: accept },
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() !== 200) throw new Error(`GitHub ${path}: ${res.getResponseCode()}`);
  return res.getContentText();
}

/** The same status the watcher sends, built from what its latest run saved. */
function statusText(now) {
  let state;
  try {
    state = JSON.parse(github("contents/state.json?ref=main", "application/vnd.github.raw+json"));
  } catch (err) {
    return "⚠️ Couldn't read the latest status from GitHub right now. Try again in a minute.";
  }
  const tz = "Asia/Kolkata";
  const health = state.health || {};
  const names = Object.keys(health).sort();
  const broken = names.filter((n) => health[n].status !== "ok").length;
  const headline = broken
    ? `⚠️ ${broken} of ${names.length} sources failing`
    : `all ${names.length} sources working`;

  const opened = Object.keys(FILM_NAMES).filter((key) =>
    Object.keys(state.alerted || {}).some((k) => k.indexOf(key + ":") === 0 && k.indexOf("booking_open") >= 0)
  ).map((key) => FILM_NAMES[key]);
  const booking = opened.length ? `Booking open: ${opened.join(", ")}` : "Booking: not open yet for either film";
  const [y, m, d] = Utilities.formatDate(now, tz, "yyyy-MM-dd").split("-").map(Number);
  const daysLeft = Math.round((Date.UTC(2026, 11, 18) - Date.UTC(y, m - 1, d)) / 86400000);

  let lastRun = "";
  try {
    const run = JSON.parse(github(`actions/workflows/${WORKFLOW}/runs?per_page=1`, "application/vnd.github+json"))
      .workflow_runs[0];
    const when = new Date(run.created_at);
    const mins = Math.round((now - when) / 60000);
    const result = run.status !== "completed" ? "running now" : run.conclusion === "success" ? "✅" : "❌ " + run.conclusion;
    lastRun = `\nLast check: ${Utilities.formatDate(when, tz, "h:mm a")} (${mins} min ago) ${result}`;
  } catch (err) {
    lastRun = "\nLast check: unknown";
  }

  const lines = names.map((n) =>
    health[n].status === "ok" ? `🟢 ${escapeHtml(n)}` : `🔴 ${escapeHtml(n)} — ${escapeHtml(health[n].status)}`
  );
  return (
    `💓 <b>Status ${Utilities.formatDate(now, tz, "h:mm a")}</b> — ${escapeHtml(headline)}\n` +
    `${escapeHtml(booking)} · ${daysLeft} days to release${lastRun}\n\n${lines.join("\n")}`
  );
}

function doGet() {
  return ContentService.createTextOutput("Relay is running.");
}

function reply(status, body) {
  return ContentService.createTextOutput(JSON.stringify({ status: status, body: body }))
    .setMimeType(ContentService.MimeType.JSON);
}
