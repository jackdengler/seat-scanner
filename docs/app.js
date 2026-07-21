/* Seat Scanner UI — vanilla JS, no build step.
 * All AMC fetching happens in GitHub Actions (browser CORS forbids it);
 * this page only talks to the GitHub API with the user's fine-grained PAT,
 * which never leaves localStorage on this device. */

"use strict";

// owner/repo derived from the Pages URL (owner.github.io/repo); fallback for local preview
const OWNER = location.hostname.endsWith(".github.io")
  ? location.hostname.split(".")[0] : "jackdengler";
const REPO = location.hostname.endsWith(".github.io")
  ? location.pathname.split("/").filter(Boolean)[0] || "seat-scanner" : "seat-scanner";
const API = "https://api.github.com";

const $ = (id) => document.getElementById(id);
let config = { watches: [], vapidPublicKey: "" };
let currentSeatmap = null;
let selected = new Set();
// Bulk browse selection: showtimeId -> {showtimeId, showDateTimeUtc, title, time}
let selectedShowings = new Map();
let currentListing = null;
// Last-rendered watches/state + the active watch-list filters, so the
// filter dropdowns can re-render without re-fetching.
let lastWatches = [];
let lastState = { watches: {} };
let watchFilter = { theatre: "", format: "" };

document.addEventListener("DOMContentLoaded", init);

function toast(msg, ms = 3500) {
  const t = $("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), ms);
}

function token() { return localStorage.getItem("pat") || ""; }

function headers() {
  const h = { Accept: "application/vnd.github+json" };
  if (token()) h.Authorization = "Bearer " + token();
  return h;
}

async function gh(path, opts = {}) {
  const res = await fetch(`${API}/repos/${OWNER}/${REPO}${path}`,
    { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
  if (!res.ok && res.status !== 404) {
    throw new Error(`GitHub API ${res.status} on ${path}`);
  }
  return res;
}

async function getFile(path, ref) {
  const refq = ref ? `ref=${ref}&` : "";
  const res = await gh(`/contents/${path}?${refq}t=${Date.now()}`,
    { headers: { Accept: "application/vnd.github.raw+json" } });
  if (res.status === 404) return null;
  return { text: await res.text(), sha: res.headers.get("etag") };
}

async function getFileWithSha(path, ref) {
  const refq = ref ? `ref=${ref}&` : "";
  const res = await gh(`/contents/${path}?${refq}t=${Date.now()}`);
  if (res.status === 404) return null;
  const j = await res.json();
  // Mirror putFile's UTF-8 encoding: atob yields raw bytes, so decodeURIComponent
  // (escape(...)) turns them back into the original Unicode. A bare atob would
  // read UTF-8 as Latin-1 and mangle non-ASCII (e.g. the em dash in labels),
  // and each read-modify-write would compound the corruption.
  return { json: JSON.parse(decodeURIComponent(escape(atob(j.content.replace(/\n/g, ""))))), sha: j.sha };
}

async function putFile(path, obj, message, sha) {
  const body = {
    message,
    content: btoa(unescape(encodeURIComponent(JSON.stringify(obj, null, 2) + "\n"))),
  };
  if (sha) body.sha = sha;
  const res = await gh(`/contents/${path}`, { method: "PUT", body: JSON.stringify(body) });
  if (res.status === 404) throw new Error("write failed — does the token have Contents read/write on this repo?");
  return res.json();
}

/* ---------- setup ---------- */

async function init() {
  $("repoLabel").textContent = `${OWNER}/${REPO}`;
  $("saveTok").onclick = saveToken;
  $("notifBtn").onclick = enableNotifications;
  $("loadBtn").onclick = () => loadSeatmap();
  $("saveWatch").onclick = saveWatch;
  $("browseBtn").onclick = browseShowtimes;
  $("bulkWatch").onclick = bulkWatch;
  $("pickSeats").onclick = pickSeatsForSelected;
  $("clearSel").onclick = clearSelection;
  $("movieFilter").oninput = () => { if (currentListing) renderShowlist(currentListing); };
  $("formatFilter").onchange = () => { if (currentListing) renderShowlist(currentListing); };
  $("theatre").value = localStorage.getItem("theatre") || "";
  $("theatre").onchange = () => localStorage.setItem("theatre", $("theatre").value.trim());
  renderPresets();
  $("date").value = todayLocal();
  if (token()) $("pat").placeholder = "saved ✓ (paste to replace)";

  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  if (ios && !standalone) $("installCard").hidden = false;

  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("sw.js"); } catch (e) { /* preview contexts */ }
    // The SW asks us to navigate when a notification is tapped and it couldn't
    // open the target itself (common for installed iOS PWAs).
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data && e.data.type === "navigate" && e.data.url) location.href = e.data.url;
    });
  }
  refreshSetupStatus();
  loadConfigAndWatches();
}

async function saveToken() {
  const v = $("pat").value.trim();
  if (!v) { toast("paste a token first"); return; }
  localStorage.setItem("pat", v);
  $("pat").value = "";
  $("setupStatus").textContent = "checking token…";
  try {
    const res = await fetch(`${API}/repos/${OWNER}/${REPO}`, { headers: headers() });
    if (res.status === 401) {
      localStorage.removeItem("pat");
      $("pat").placeholder = "github_pat_…";
      toast("GitHub rejected the token (401) — re-copy the full value; if it's gone, generate a new one", 8000);
    } else if (res.status === 404 || res.status === 403) {
      toast("Token works but can't see this repo — grant it access to " + OWNER + "/" + REPO, 8000);
    } else {
      const j = await res.json();
      if (!(j.permissions && j.permissions.push)) {
        toast("Token can read but not write — set Contents to read/write", 8000);
      } else {
        $("pat").placeholder = "saved ✓ (paste to replace)";
        toast("token works ✓");
        loadConfigAndWatches();
      }
    }
  } catch (e) {
    toast("couldn't reach GitHub: " + e.message, 6000);
  }
  refreshSetupStatus();
}

function refreshSetupStatus() {
  const bits = [];
  bits.push(token() ? '<span class="ok">token saved</span>' : '<span class="warn">no token yet</span>');
  if (!("Notification" in window)) bits.push('<span class="warn">notifications unsupported here (install as PWA on iOS)</span>');
  else if (Notification.permission === "granted") bits.push('<span class="ok">notifications on</span>');
  else bits.push('<span class="warn">notifications off</span>');
  $("setupStatus").innerHTML = bits.join(" · ");
}

function b64ToBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function enableNotifications() {
  try {
    if (!("Notification" in window) || !("PushManager" in window)) {
      toast("Not supported here — on iPhone, Add to Home Screen first, then open the app.");
      return;
    }
    if (!config.vapidPublicKey) await loadConfigAndWatches();
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("permission denied"); refreshSetupStatus(); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToBytes(config.vapidPublicKey),
    });
    const cur = await getFileWithSha("subscriptions.json");
    const obj = cur ? cur.json : { subscriptions: [] };
    const json = sub.toJSON();
    if (!obj.subscriptions.some((s) => s.endpoint === json.endpoint)) {
      obj.subscriptions.push(json);
      await putFile("subscriptions.json", obj, "register push subscription", cur && cur.sha);
    }
    toast("notifications enabled ✓");
  } catch (e) {
    toast("enable failed: " + e.message, 6000);
  }
  refreshSetupStatus();
}

/* ---------- add a watch ---------- */

function showtimeIdFromInput(v) {
  const m = v.match(/showtimes\/(\d+)/) || v.match(/^(\d{6,})$/);
  return m ? m[1] : null;
}

async function defaultBranch() {
  if (defaultBranch.cached) return defaultBranch.cached;
  const res = await gh("");
  const j = await res.json();
  defaultBranch.cached = j.default_branch;
  return j.default_branch;
}

// Make sure the poll workflow is running so a freshly added watch actually
// gets checked. poll.yml disables itself once every watch has finished
// (see the "Disable polling when no active watches remain" step), so a watch
// added afterwards would sit at "first check pending" forever until the
// workflow is manually re-enabled. Re-enable it (no-op if already active) and
// kick a run so the first check lands promptly.
async function ensurePolling() {
  const wf = "poll.yml";
  await gh(`/actions/workflows/${wf}/enable`, { method: "PUT" });
  const ref = await defaultBranch();
  await gh(`/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref }),
  });
}

async function loadSeatmap(sidArg) {
  const sid = sidArg || showtimeIdFromInput($("showtime").value.trim());
  if (!sid) { toast("paste an AMC showtime link or numeric ID"); return; }
  if (!token()) { toast("save your token first"); return; }
  const btn = $("loadBtn");
  btn.disabled = true;
  const status = (m) => ($("loadStatus").textContent = m);
  try {
    status("asking GitHub Actions to fetch the seat map…");
    const ref = await defaultBranch();
    const res = await gh(`/actions/workflows/fetch-seatmap.yml/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref, inputs: { showtimeId: sid } }),
    });
    if (!res.ok) throw new Error("dispatch failed — check token Actions permission");
    const started = Date.now();
    while (Date.now() - started < 4 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 6000));
      status(`waiting for the fetch to land… ${Math.round((Date.now() - started) / 1000)}s`);
      const f = await getFile(`seatmap-${sid}.json`, "data");
      if (f) {
        const sm = JSON.parse(f.text);
        if (new Date(sm.fetchedAtUtc).getTime() > started - 60_000) {
          currentSeatmap = sm;
          renderPicker(sm);
          status("");
          return;
        }
      }
    }
    status("timed out — check the fetch-seatmap run in the Actions tab.");
  } catch (e) {
    status("failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------- browse a theatre by date ---------- */

function todayLocal() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Path after /movie-theatres/ (market/slug), from a full URL or bare path.
function theatrePath(v) {
  const m = v.match(/movie-theatres\/([a-z0-9-]+\/[a-z0-9-]+)/i)
    || v.match(/movie-theatres\/([a-z0-9-]+)/i);
  if (m) return m[1];
  const cleaned = v.trim().replace(/^\/+|\/+$/g, "").split("?")[0];
  return /^[a-z0-9-]+(\/[a-z0-9-]+)?$/i.test(cleaned) ? cleaned : null;
}

// One-tap LA theatres so you don't have to paste a link. `path` is the
// market/slug after /movie-theatres/ — note CityWalk's listing lives under its
// internal "universal-cinema-an-amc-theatre" slug (the marketing URL slug
// doesn't match the ids the showtimes markup is keyed on).
const THEATRE_PRESETS = [
  { label: "Century City", path: "los-angeles/amc-century-city-15" },
  { label: "CityWalk", path: "los-angeles/universal-cinema-an-amc-theatre" },
  { label: "The Grove", path: "los-angeles/amc-the-grove-14" },
];

function renderPresets() {
  const host = $("presets");
  if (!host) return;
  host.innerHTML = "";
  const lbl = document.createElement("span");
  lbl.className = "muted presetlbl";
  lbl.textContent = "LA quick picks:";
  host.appendChild(lbl);
  for (const p of THEATRE_PRESETS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset";
    b.textContent = p.label;
    b.onclick = () => {
      const url = "https://www.amctheatres.com/movie-theatres/" + p.path;
      $("theatre").value = url;
      localStorage.setItem("theatre", url);
      browseShowtimes();
    };
    host.appendChild(b);
  }
}

async function browseShowtimes() {
  const theatre = theatrePath($("theatre").value.trim());
  const date = $("date").value || todayLocal();
  // AMC needs the market segment too (market/slug), not a bare slug.
  if (!theatre || !theatre.includes("/")) {
    toast("paste your theatre's full AMC page link (…/movie-theatres/<city>/<theatre>)", 6000);
    return;
  }
  if (!token()) { toast("save your token first"); return; }
  localStorage.setItem("theatre", $("theatre").value.trim());
  const days = parseInt($("days").value, 10) || 1;
  const btn = $("browseBtn");
  btn.disabled = true;
  $("showlist").innerHTML = "";
  const status = (m) => ($("browseStatus").textContent = m);
  try {
    status("asking GitHub Actions to fetch showtimes…");
    const ref = await defaultBranch();
    const res = await gh(`/actions/workflows/fetch-showtimes.yml/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref, inputs: { theatre, date, days: String(days) } }),
    });
    if (!res.ok) throw new Error("dispatch failed — check token Actions permission");
    const started = Date.now();
    // multi-day fetches take longer (one AMC request per day)
    const timeoutMs = Math.min(9, 3 + days) * 60 * 1000;
    while (Date.now() - started < timeoutMs) {
      await new Promise((r) => setTimeout(r, 5000));
      const secs = Math.round((Date.now() - started) / 1000);
      status(`waiting for ${days === 1 ? "showtimes" : days + " days of showtimes"}… ${secs}s`);
      const f = await getFile("browse.json", "data");
      if (f) {
        const listing = JSON.parse(f.text);
        if (listing.theatre === theatre && listing.date === date
            && String(listing.days || 1) === String(days)
            && new Date(listing.fetchedAtUtc).getTime() > started - 60_000) {
          status("");
          populateFormatFilter(listing);
          renderShowlist(listing);
          backfillWatchMeta(listing);
          return;
        }
      }
    }
    status("timed out — check the fetch-showtimes run in the Actions tab.");
  } catch (e) {
    status("failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

function dayKey(utc) {
  const d = new Date(utc);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function dayLabel(utc) {
  return new Date(utc).toLocaleDateString(undefined,
    { weekday: "short", month: "short", day: "numeric" });
}

// Fill the format dropdown from the formats present in a fresh listing,
// keeping the current pick if it still exists. Called once per new listing,
// not on every filter re-render.
function populateFormatFilter(listing) {
  const sel = $("formatFilter");
  const prev = sel.value;
  const fmts = new Set();
  for (const mv of listing.movies || []) {
    for (const s of mv.showings) if (s.format) fmts.add(s.format);
  }
  const sorted = [...fmts].sort();
  sel.innerHTML = '<option value="">All formats</option>' +
    sorted.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
  sel.value = sorted.includes(prev) ? prev : "";
}

function renderShowlist(listing) {
  currentListing = listing;
  const host = $("showlist");
  host.innerHTML = "";
  const movies = listing.movies || [];
  if (!movies.length) {
    $("browseHint").hidden = true;
    $("filterRow").hidden = true;
    host.innerHTML = '<p class="muted">No showtimes listed for those dates.</p>';
    return;
  }
  $("browseHint").hidden = false;
  $("filterRow").hidden = false;

  const filter = $("movieFilter").value.trim().toLowerCase();
  const fmtFilter = $("formatFilter").value;
  const now = Date.now();
  // day headers only when the listing actually spans more than one day
  const allDays = new Set();
  for (const mv of movies) for (const s of mv.showings) allDays.add(dayKey(s.showDateTimeUtc));
  const multiDay = allDays.size > 1;

  let shown = 0;
  for (const mv of movies) {
    if (filter && !mv.title.toLowerCase().includes(filter)) continue;
    const showings = fmtFilter
      ? mv.showings.filter((s) => (s.format || "") === fmtFilter)
      : mv.showings;
    if (!showings.length) continue;
    shown++;
    const div = document.createElement("div");
    div.className = "movie";
    div.innerHTML = `<b>${esc(mv.title)}</b>`;

    const byDay = {};
    for (const s of showings) (byDay[dayKey(s.showDateTimeUtc)] ||= []).push(s);
    for (const dk of Object.keys(byDay).sort()) {
      const dayShowings = byDay[dk];
      if (multiDay) {
        const dh = document.createElement("div");
        dh.className = "dayhdr";
        dh.textContent = dayLabel(dayShowings[0].showDateTimeUtc);
        div.appendChild(dh);
      }
      const byFmt = {};
      for (const s of dayShowings) (byFmt[s.format || ""] ||= []).push(s);
      for (const [fmt, showings] of Object.entries(byFmt)) {
        if (fmt) {
          const f = document.createElement("div");
          f.className = "fmt";
          f.textContent = fmt;
          div.appendChild(f);
        }
        const times = document.createElement("div");
        times.className = "times";
        for (const s of showings) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "time";
          b.textContent = s.time;
          if (new Date(s.showDateTimeUtc).getTime() < now) b.classList.add("past");
          // tapping selects/deselects; selection survives re-renders/date changes
          if (selectedShowings.has(s.showtimeId)) b.classList.add("sel");
          b.onclick = () => toggleShowing(s, mv.title, b);
          times.appendChild(b);
        }
        div.appendChild(times);
      }
    }
    host.appendChild(div);
  }
  if (!shown) host.innerHTML = '<p class="muted">No shows match those filters.</p>';
  updateBulkBar();
}

function toggleShowing(s, title, el) {
  if (selectedShowings.has(s.showtimeId)) {
    selectedShowings.delete(s.showtimeId);
    el.classList.remove("sel");
  } else {
    selectedShowings.set(s.showtimeId, {
      showtimeId: s.showtimeId,
      showDateTimeUtc: s.showDateTimeUtc,
      title,
      time: s.time,
      format: s.format || "",
    });
    el.classList.add("sel");
  }
  updateBulkBar();
}

function updateBulkBar() {
  const n = selectedShowings.size;
  $("bulkbar").hidden = n === 0;
  $("selCount").textContent = n;
  $("pickSeats").disabled = n !== 1;
}

function clearSelection() {
  selectedShowings.clear();
  document.querySelectorAll(".time.sel").forEach((el) => el.classList.remove("sel"));
  updateBulkBar();
}

// Older watches were saved before we stored theatre/format. When a browse
// listing loads, stamp those fields onto any existing watch it covers (matched
// by showtime id) so they become filterable too. Best-effort and silent.
async function backfillWatchMeta(listing) {
  if (!listing || !listing.movies) return;
  const theatre = prettyTheatre(listing.theatreSlug || listing.theatre || "");
  const fmtBySid = new Map();
  for (const mv of listing.movies)
    for (const s of mv.showings) fmtBySid.set(String(s.showtimeId), s.format || "");

  let changed = false;
  for (const w of config.watches || []) {
    const sid = String(w.showtimeId);
    if (!fmtBySid.has(sid)) continue;
    if (theatre && w.theatre !== theatre) { w.theatre = theatre; changed = true; }
    const f = fmtBySid.get(sid);
    if (f && w.format !== f) { w.format = f; changed = true; }
  }
  if (!changed) return;

  renderWatches(config.watches, lastState);   // reflect immediately
  if (!token()) return;                        // can't persist without a token
  try {
    const cur = await getFileWithSha("config.json");
    if (!cur) return;
    // Re-apply onto the latest config so we don't clobber a concurrent edit.
    for (const w of cur.json.watches || []) {
      const sid = String(w.showtimeId);
      if (!fmtBySid.has(sid)) continue;
      if (theatre) w.theatre = theatre;
      const f = fmtBySid.get(sid);
      if (f) w.format = f;
    }
    await putFile("config.json", cur.json, "backfill watch theatre/format", cur.sha);
    config = cur.json;
  } catch (e) {
    /* leave the in-memory enrichment; it'll retry on the next browse */
  }
}

// "amc-boston-common-19" -> "AMC Boston Common 19", for the theatre filter.
function prettyTheatre(slug) {
  if (!slug) return "";
  return String(slug).split(/[-_]/).filter(Boolean)
    .map((w) => (w.toLowerCase() === "amc" ? "AMC" : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

function watchLabel(showing) {
  const when = new Date(showing.showDateTimeUtc).toLocaleString(undefined,
    { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return `${showing.title} — ${when}`;
}

async function bulkWatch() {
  const showings = [...selectedShowings.values()];
  if (!showings.length) return;
  if (!token()) { toast("save your token first"); return; }
  const rows = $("brows").value.split(",").map((s) => s.trim()).filter(Boolean);
  const adj = Math.max(1, parseInt($("badj").value, 10) || 1);
  const exclude = [];
  if ($("bexWheel").checked) exclude.push("Wheelchair");
  if ($("bexComp").checked) exclude.push("Companion");
  const btn = $("bulkWatch");
  btn.disabled = true;
  try {
    const cur = await getFileWithSha("config.json");
    const obj = cur ? cur.json : { watches: [] };
    const theatre = prettyTheatre(
      (currentListing && (currentListing.theatreSlug || currentListing.theatre)) || "");
    for (const s of showings) {
      const watch = {
        showtimeId: s.showtimeId,
        showtimeIso: s.showDateTimeUtc,
        label: watchLabel(s),
        theatre,
        format: s.format || "",
        watchedSeats: [],
        watchedRows: rows,
        adjacentRequired: adj,
        excludeTypes: exclude,
      };
      obj.watches = obj.watches.filter((w) => String(w.showtimeId) !== String(watch.showtimeId));
      obj.watches.push(watch);
    }
    await putFile("config.json", obj, `watch ${showings.length} show(s)`, cur && cur.sha);
    config = obj;
    try {
      await ensurePolling();
      toast(`watching ${showings.length} show(s) ✓ — first check runs within a minute`);
    } catch (e) {
      toast(`watching ${showings.length} show(s) ✓ — but couldn't start the poller (` +
        e.message + "); enable the poll workflow in the Actions tab", 8000);
    }
    clearSelection();
    renderWatches(obj.watches, await loadState());
  } catch (e) {
    toast("save failed: " + e.message, 6000);
  } finally {
    btn.disabled = false;
  }
}

function pickSeatsForSelected() {
  if (selectedShowings.size !== 1) return;
  const [s] = selectedShowings.values();
  loadSeatmap(String(s.showtimeId));
}

function renderPicker(sm) {
  selected = new Set();
  const when = sm.showDateTimeUtc ? new Date(sm.showDateTimeUtc) : null;
  const meta = [sm.movie, sm.theatre, when && when.toLocaleString()].filter(Boolean).join(" · ");
  $("showMeta").textContent = meta || `showtime ${sm.showtimeId}`;
  $("label").value = [sm.movie || `#${sm.showtimeId}`,
    when && when.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })]
    .filter(Boolean).join(" ");

  const byPos = {};
  for (const s of sm.seats) byPos[`${s.row},${s.column}`] = s;
  const grid = $("grid");
  grid.innerHTML = "";
  for (let r = 1; r <= sm.rows; r++) {
    const seatsInRow = sm.seats.filter((s) => s.row === r);
    if (!seatsInRow.length) continue;
    const div = document.createElement("div");
    div.className = "seatrow";
    const lab = document.createElement("span");
    lab.className = "rowlabel";
    lab.textContent = (seatsInRow[0].name.match(/^[A-Za-z]+/) || [""])[0];
    div.appendChild(lab);
    for (let c = 1; c <= sm.columns; c++) {
      const s = byPos[`${r},${c}`];
      const el = document.createElement("button");
      el.type = "button";
      el.className = "seat";
      if (!s) { el.classList.add("gap"); }
      else {
        el.textContent = s.name.replace(/^[A-Za-z]+/, "");
        if (/Wheelchair|Companion/.test(s.type)) el.classList.add("special");
        if (!s.available) el.classList.add("taken");
        el.onclick = () => {
          if (selected.has(s.name)) { selected.delete(s.name); el.classList.remove("sel"); }
          else { selected.add(s.name); el.classList.add("sel"); }
        };
      }
      div.appendChild(el);
    }
    grid.appendChild(div);
  }
  $("picker").hidden = false;
  $("picker").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveWatch() {
  if (!currentSeatmap) return;
  const sm = currentSeatmap;
  const exclude = [];
  if ($("exWheel").checked) exclude.push("Wheelchair");
  if ($("exComp").checked) exclude.push("Companion");
  const watch = {
    showtimeId: sm.showtimeId,
    showtimeIso: sm.showDateTimeUtc,
    label: $("label").value.trim() || `showtime ${sm.showtimeId}`,
    theatre: sm.theatre || "",
    format: sm.format || "",
    watchedSeats: [...selected],
    watchedRows: $("rows").value.split(",").map((s) => s.trim()).filter(Boolean),
    adjacentRequired: Math.max(1, parseInt($("adj").value, 10) || 1),
    excludeTypes: exclude,
  };
  try {
    const cur = await getFileWithSha("config.json");
    const obj = cur ? cur.json : { watches: [] };
    obj.watches = obj.watches.filter((w) => String(w.showtimeId) !== String(watch.showtimeId));
    obj.watches.push(watch);
    await putFile("config.json", obj, `watch ${watch.label}`, cur && cur.sha);
    config = obj;
    try {
      await ensurePolling();
      toast("watching ✓ — first check runs within a minute");
    } catch (e) {
      toast("watching ✓ — but couldn't start the poller (" + e.message +
        "); enable the poll workflow in the Actions tab", 8000);
    }
    $("picker").hidden = true;
    $("showtime").value = "";
    renderWatches(obj.watches, await loadState());
  } catch (e) {
    toast("save failed: " + e.message, 6000);
  }
}

/* ---------- watch list ---------- */

async function loadState() {
  try {
    const f = await getFile("state.json", "data");
    return f ? JSON.parse(f.text) : { watches: {} };
  } catch (e) {
    return { watches: {} };
  }
}

async function loadConfigAndWatches() {
  try {
    const f = await getFile("config.json");
    if (f) config = JSON.parse(f.text);
    const state = await loadState();
    renderWatches(config.watches || [], state);
    renderAlerts(state);
    repairCorruptedLabels();
  } catch (e) {
    $("watches").textContent = "couldn't load: " + e.message;
  }
}

// The poller writes newest-first "alerts" into state.json each time seats open.
// Render them as a book-now list so a missed notification is never a dead end.
function renderAlerts(state) {
  const card = $("alertsCard");
  const host = $("alerts");
  if (!card || !host) return;
  const dismissed = JSON.parse(localStorage.getItem("dismissedAlerts") || "[]");
  const alerts = ((state && state.alerts) || []).filter(
    (a) => !dismissed.includes(a.key || `${a.sid}-${a.at}`));
  if (!alerts.length) { card.hidden = true; return; }
  card.hidden = false;
  host.innerHTML = "";
  for (const a of alerts) {
    const sid = String(a.sid || "").replace(/[^0-9]/g, "");
    const div = document.createElement("div");
    div.className = "alert";
    div.innerHTML =
      `<b>${esc(a.label || ("showtime " + sid))}</b>` +
      `<div class="seats">Open seats: ${esc(a.seats || "—")}</div>` +
      `<div class="muted">${a.at ? "alerted " + timeAgo(a.at) : ""}</div>`;
    const book = document.createElement("a");
    book.className = "book";
    book.href = "https://www.amctheatres.com/showtimes/" + sid + "/seats";
    book.textContent = "🎟 Book in the AMC app";
    const dismiss = document.createElement("button");
    dismiss.className = "dismiss";
    dismiss.textContent = "dismiss";
    dismiss.onclick = () => {
      const key = a.key || `${a.sid}-${a.at}`;
      const d = JSON.parse(localStorage.getItem("dismissedAlerts") || "[]");
      if (!d.includes(key)) d.push(key);
      localStorage.setItem("dismissedAlerts", JSON.stringify(d.slice(-100)));
      renderAlerts(state);
    };
    const actions = document.createElement("div");
    actions.appendChild(book);
    actions.appendChild(dismiss);
    div.appendChild(actions);
    host.appendChild(div);
  }
}

// A run of two-or-more consecutive high bytes is the signature of a label whose
// em dash was mangled by the old bare-atob decode (see getFileWithSha).
function repairLabel(label) {
  const DASH = String.fromCharCode(0x2014); // em dash
  let first = -1, last = -1, run = 0, corrupt = false;
  for (let k = 0; k < label.length; k++) {
    const c = label.charCodeAt(k);
    if (c > 0x7f) { if (first < 0) first = k; last = k; }
    if (c >= 0x80 && c <= 0xff) { run++; if (run >= 2) corrupt = true; } else { run = 0; }
  }
  if (!corrupt || first < 0) return label;   // no mangled run — leave it alone
  const title = label.slice(0, first).trim();
  const rest = label.slice(last + 1).trim();
  return rest ? title + " " + DASH + " " + rest : title;
}

// One-time cleanup of labels corrupted by the earlier encoding bug. The garbled
// middle (a mangled em dash) is dropped and the clean title/date rejoined.
async function repairCorruptedLabels() {
  const fixes = (config.watches || []).map((w) => repairLabel(w.label || ""));
  if (!fixes.some((fixed, i) => fixed !== (config.watches[i].label || ""))) return;
  config.watches.forEach((w, i) => (w.label = fixes[i]));
  renderWatches(config.watches, lastState);
  if (!token()) return;
  try {
    const cur = await getFileWithSha("config.json");
    if (!cur) return;
    for (const w of cur.json.watches || []) w.label = repairLabel(w.label || "");
    await putFile("config.json", cur.json, "repair corrupted watch labels", cur.sha);
    config = cur.json;
    renderWatches(config.watches, await loadState());
  } catch (e) {
    /* in-memory repair still stands; it'll retry next load */
  }
}

function renderWatches(watches, state) {
  lastWatches = watches;
  lastState = state;
  const host = $("watches");
  host.innerHTML = "";
  if (!watches.length) { host.textContent = "No watches yet."; return; }

  const theatres = [...new Set(watches.map((w) => w.theatre).filter(Boolean))].sort();
  const formats = [...new Set(watches.map((w) => w.format).filter(Boolean))].sort();
  // Drop a stale selection (e.g. after removing the last watch of a theatre).
  if (watchFilter.theatre && !theatres.includes(watchFilter.theatre)) watchFilter.theatre = "";
  if (watchFilter.format && !formats.includes(watchFilter.format)) watchFilter.format = "";

  // Only offer a dropdown when there's actually more than one value to pick.
  if (theatres.length > 1 || formats.length > 1) {
    const bar = document.createElement("div");
    bar.className = "watchfilter";
    const mkSel = (key, all, opts) => {
      const sel = document.createElement("select");
      sel.innerHTML = `<option value="">${all}</option>` +
        opts.map((o) => `<option value="${esc(o)}"${o === watchFilter[key] ? " selected" : ""}>${esc(o)}</option>`).join("");
      sel.onchange = () => { watchFilter[key] = sel.value; renderWatches(lastWatches, lastState); };
      return sel;
    };
    if (theatres.length > 1) bar.appendChild(mkSel("theatre", "All theatres", theatres));
    if (formats.length > 1) bar.appendChild(mkSel("format", "All formats", formats));
    host.appendChild(bar);
  }

  const filtered = watches.filter((w) =>
    (!watchFilter.theatre || w.theatre === watchFilter.theatre) &&
    (!watchFilter.format || w.format === watchFilter.format));

  const isFiltered = !!(watchFilter.theatre || watchFilter.format);
  const header = document.createElement("div");
  header.className = "watchhead";
  const count = document.createElement("span");
  count.className = "muted";
  count.textContent = isFiltered
    ? `${filtered.length} of ${watches.length} watches`
    : `${watches.length} watch${watches.length === 1 ? "" : "es"}`;
  header.appendChild(count);
  if (filtered.length) {
    const rm = document.createElement("button");
    rm.className = "linkbtn removeall";
    rm.textContent = isFiltered ? `Remove these ${filtered.length}` : "Remove all";
    rm.onclick = () => removeMany(filtered.map((w) => w.showtimeId),
      isFiltered ? "remove filtered watches" : "remove all watches");
    header.appendChild(rm);
  }
  host.appendChild(header);

  if (!filtered.length) {
    const none = document.createElement("div");
    none.className = "muted";
    none.textContent = "No watches match those filters.";
    host.appendChild(none);
    return;
  }

  for (const w of filtered) {
    const ws = (state.watches || {})[String(w.showtimeId)] || {};
    const div = document.createElement("div");
    div.className = "watch";
    const pills = [];
    if (w.theatre) pills.push("📍 " + w.theatre);
    if (w.format) pills.push(w.format);
    if (w.watchedSeats && w.watchedSeats.length) pills.push("seats " + w.watchedSeats.join(" "));
    else if (w.watchedRows && w.watchedRows.length) pills.push("rows " + w.watchedRows.join(" "));
    else pills.push("anywhere");
    pills.push(`${w.adjacentRequired || 1}+ adjacent`);
    let status;
    if (ws.done) status = '<span class="muted">finished</span>';
    else if ((ws.consecutiveFailures || 0) >= 3) status = '<span class="err">broken — see Actions logs</span>';
    else if (ws.lastCheckedAt) status = `<span class="ok">checked ${timeAgo(ws.lastCheckedAt)}</span>`;
    else status = '<span class="warn">first check pending</span>';
    if ((ws.notifiedSignatures || []).length) status += ` · ${ws.notifiedSignatures.length} match alert(s) sent`;
    div.innerHTML =
      `<div class="whead"><b>${esc(w.label)}</b><button class="del" aria-label="Remove watch">✕ Remove</button></div>` +
      `<div class="wpills">${pills.map((r) => `<span class="pill">${esc(r)}</span>`).join("")}</div>` +
      `<div class="muted wstatus">${status}</div>`;
    div.querySelector(".del").onclick = () => removeWatch(w.showtimeId);
    host.appendChild(div);
  }
}

async function removeWatch(sid) {
  await removeMany([sid], `unwatch ${sid}`);
}

// Remove any number of watches in a single config write. Confirms first when
// clearing more than one so a stray tap can't wipe the list.
async function removeMany(sids, desc) {
  if (!sids || !sids.length) return;
  if (sids.length > 1 &&
      !confirm(`Remove ${sids.length} watches? This can't be undone.`)) return;
  try {
    const cur = await getFileWithSha("config.json");
    const drop = new Set(sids.map(String));
    cur.json.watches = (cur.json.watches || [])
      .filter((w) => !drop.has(String(w.showtimeId)));
    await putFile("config.json", cur.json, desc, cur.sha);
    config = cur.json;
    renderWatches(config.watches, await loadState());
    toast(sids.length === 1 ? "removed" : `removed ${sids.length} watches`);
  } catch (e) {
    toast("remove failed: " + e.message, 6000);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function timeAgo(iso) {
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  return `${Math.round(min / 60)}h ago`;
}
