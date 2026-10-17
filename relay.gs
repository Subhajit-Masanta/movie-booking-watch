/**
 * Google Apps Script helper for the booking watcher. Runs free on Google's side.
 *
 * It does two jobs:
 *  1. Starts the GitHub "Booking watch" run every 5 minutes. GitHub's own
 *     schedule is often hours late on free repos.
 *  2. Relays requests to INOX and District, which block GitHub's servers.
 *
 * Settings live in Project Settings -> Script Properties, never in this code:
 *   GITHUB_TOKEN  fine-grained token with "Actions: Read and write" on the repo
 *   RELAY_KEY     any long random text; the same text goes in GitHub's RELAY_KEY secret
 *
 * Setup steps: see "Google helper" in README.md.
 */

const REPO = "Subhajit-Masanta/movie-booking-watch";
const WORKFLOW = "watch.yml";
const ALLOWED = /(^|\.)(inoxmovies\.com|pvrcinemas\.com|district\.in|bookmyshow\.com)$/;

function prop(name) {
  return PropertiesService.getScriptProperties().getProperty(name) || "";
}

// ---------------------------------------------------------------- job 1

/** Run once from the editor: replaces any old schedule with one every 5 minutes. */
function setup() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === "startWatch")
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger("startWatch").timeBased().everyMinutes(5).create();
  startWatch();
  Logger.log("Scheduled every 5 minutes, and started one run now.");
}

/** Asks GitHub to start the watcher workflow. */
function startWatch() {
  if (new Date() > new Date("2026-12-26T00:00:00+05:30")) return; // films are out
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

// ---------------------------------------------------------------- job 2

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
  if (!prop("RELAY_KEY") || req.key !== prop("RELAY_KEY")) return reply(401, "wrong key");

  const url = String(req.url || "");
  const host = url.replace(/^https:\/\//, "").split(/[/?#]/)[0];
  if (!url.startsWith("https://") || !ALLOWED.test(host)) return reply(400, "host not allowed");

  const options = {
    method: req.method === "post" ? "post" : "get",
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
