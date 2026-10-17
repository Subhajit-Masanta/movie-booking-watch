/**
 * Google Apps Script helper for the booking watcher. Runs free on Google's side.
 *
 * It does two jobs:
 *  1. Every 5 minutes it fetches the District pages the watcher asked for and
 *     keeps a fresh copy, then starts the GitHub "Booking watch" run. GitHub's
 *     own schedule is often hours late on free repos.
 *  2. Hands those copies to the watcher on request. District refuses pages
 *     requested on GitHub's behalf, but answers this script's own timer.
 *
 * Settings live in Project Settings -> Script Properties, never in this code:
 *   GITHUB_TOKEN  fine-grained token with "Actions: Read and write" on the repo
 *   RELAY_KEY     any long random text; the same text goes in GitHub's RELAY_KEY secret
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

function doGet() {
  return ContentService.createTextOutput("Relay is running.");
}

function reply(status, body) {
  return ContentService.createTextOutput(JSON.stringify({ status: status, body: body }))
    .setMimeType(ContentService.MimeType.JSON);
}
