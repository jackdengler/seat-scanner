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
  return { json: JSON.parse(atob(j.content.replace(/\n/g, ""))), sha: j.sha };
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
  $("theatre").value = localStorage.getItem("theatre") || "";
  $("theatre").onchange = () => localStorage.setItem("theatre", $("theatre").value.trim());
  $("date").value = todayLocal();
  if (token()) $("pat").placeholder = "saved ✓ (paste to replace)";

  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  if (ios && !standalone) $("installCard").hidden = false;

  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("sw.js"); } catch (e) { /* preview contexts */ }
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
  const btn = $("browseBtn");
  btn.disabled = true;
  $("showlist").innerHTML = "";
  const status = (m) => ($("browseStatus").textContent = m);
  try {
    status("asking GitHub Actions to fetch showtimes…");
    const ref = await defaultBranch();
    const res = await gh(`/actions/workflows/fetch-showtimes.yml/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref, inputs: { theatre, date } }),
    });
    if (!res.ok) throw new Error("dispatch failed — check token Actions permission");
    const started = Date.now();
    while (Date.now() - started < 4 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 5000));
      status(`waiting for showtimes… ${Math.round((Date.now() - started) / 1000)}s`);
      const f = await getFile("browse.json", "data");
      if (f) {
        const listing = JSON.parse(f.text);
        if (listing.theatre === theatre && listing.date === date
            && new Date(listing.fetchedAtUtc).getTime() > started - 60_000) {
          status("");
          renderShowlist(listing);
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

function renderShowlist(listing) {
  const host = $("showlist");
  host.innerHTML = "";
  if (!listing.movies || !listing.movies.length) {
    host.innerHTML = '<p class="muted">No showtimes listed for that date.</p>';
    return;
  }
  const now = Date.now();
  for (const mv of listing.movies) {
    const div = document.createElement("div");
    div.className = "movie";
    div.innerHTML = `<b>${esc(mv.title)}</b>`;
    // group this movie's showings by format
    const byFmt = {};
    for (const s of mv.showings) (byFmt[s.format || ""] ||= []).push(s);
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
        b.onclick = () => loadSeatmap(String(s.showtimeId));
        times.appendChild(b);
      }
      div.appendChild(times);
    }
    host.appendChild(div);
  }
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
    toast("watching ✓ — first check runs within a minute");
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
    renderWatches(config.watches || [], await loadState());
  } catch (e) {
    $("watches").textContent = "couldn't load: " + e.message;
  }
}

function renderWatches(watches, state) {
  const host = $("watches");
  if (!watches.length) { host.textContent = "No watches yet."; return; }
  host.innerHTML = "";
  for (const w of watches) {
    const ws = (state.watches || {})[String(w.showtimeId)] || {};
    const div = document.createElement("div");
    div.className = "watch";
    const rules = [];
    if (w.watchedSeats && w.watchedSeats.length) rules.push("seats " + w.watchedSeats.join(" "));
    else if (w.watchedRows && w.watchedRows.length) rules.push("rows " + w.watchedRows.join(" "));
    else rules.push("anywhere");
    rules.push(`${w.adjacentRequired || 1}+ adjacent`);
    let status;
    if (ws.done) status = '<span class="muted">finished</span>';
    else if ((ws.consecutiveFailures || 0) >= 3) status = '<span class="err">broken — see Actions logs</span>';
    else if (ws.lastCheckedAt) status = `<span class="ok">checked ${timeAgo(ws.lastCheckedAt)}</span>`;
    else status = '<span class="warn">first check pending</span>';
    if ((ws.notifiedSignatures || []).length) status += ` · ${ws.notifiedSignatures.length} match alert(s) sent`;
    div.innerHTML = `<button class="del">remove</button><b>${esc(w.label)}</b><br>` +
      rules.map((r) => `<span class="pill">${esc(r)}</span>`).join("") +
      `<br><span class="muted">${status}</span>`;
    div.querySelector(".del").onclick = () => removeWatch(w.showtimeId);
    host.appendChild(div);
  }
}

async function removeWatch(sid) {
  try {
    const cur = await getFileWithSha("config.json");
    cur.json.watches = cur.json.watches.filter((w) => String(w.showtimeId) !== String(sid));
    await putFile("config.json", cur.json, `unwatch ${sid}`, cur.sha);
    config = cur.json;
    renderWatches(config.watches, await loadState());
    toast("removed");
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
