// Bookmarklet body: run on an AMC theatre page in your own browser to read its
// showtimes and hand them to Seat Scanner. AMC's Cloudflare blocks GitHub's
// runners from the showtimes page, but a real browser session passes, and
// same-origin fetches from it reuse that session.
//
// The app builds the bookmark from this file (replacing __APP_URL__), so this
// stays the single source. The parser is a port of scripts/amc.py
// parse_showtimes / merge_showtimes; keep the two in step.

(() => {
  const FLIGHT_RE = /self\.__next_f\.push\(\[1,\s*"((?:\\.|[^"\\])*)"\]\)/g;
  const SHOWTIME_RE =
    /\{"showtimeId":(\d+),.*?"showDateTimeUtc":"([^"]+)".*?"time":"([^"]+)","amPm":"([^"]+)"\}/g;

  function decodeFlight(html) {
    let out = "";
    for (const m of html.matchAll(FLIGHT_RE)) out += JSON.parse('"' + m[1] + '"');
    return out;
  }

  function theatreSlug(path) {
    const m = path.trim().replace(/^\/+|\/+$/g, "").split("?")[0].match(/[a-z0-9-]+$/);
    return m ? m[0] : path;
  }

  function movieTitles(flight) {
    const titles = {};
    const add = (re) => {
      for (const m of flight.matchAll(re)) if (!(m[1] in titles)) titles[m[1]] = m[2];
    };
    add(/\/movies\/([a-z0-9-]+)"[^\]]{0,160}?"children":(?:"|\[")(?!\$)([^"]{1,90})"/g);
    add(/\/movies\/([a-z0-9-]+)"(?:[\s\S]{0,500}?)"alt":"(?!\$)([^"]{1,90})"/g);
    return titles;
  }

  function prettySlug(slug) {
    return slug.replace(/-\d+$/, "").split("-")
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ") || slug;
  }

  function parseShowtimes(html, path) {
    const flight = decodeFlight(html);
    if (!flight) throw new Error("no showtimes data on the page");
    const slug = theatreSlug(path);
    const titles = movieTitles(flight);
    const esc = slug.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const headerRe = new RegExp('"id":"([a-z0-9-]+?-\\d+)-' + esc + '-([a-z0-9]+)-\\d+"', "g");

    const tokens = [];
    for (const m of flight.matchAll(headerRe)) {
      const end = m.index + m[0].length;
      const fmt = flight.slice(end, end + 400).match(/"children":"([^"]{1,40})"/);
      tokens.push([m.index, "hdr", [m[1], fmt ? fmt[1] : m[2]]]);
    }
    for (const m of flight.matchAll(SHOWTIME_RE)) tokens.push([m.index, "show", m.slice(1)]);
    tokens.sort((a, b) => a[0] - b[0]);

    const movies = new Map();
    let current = null, currentFmt = null;
    for (const [, kind, data] of tokens) {
      if (kind === "hdr") {
        [current, currentFmt] = data;
        if (!movies.has(current)) {
          movies.set(current, { slug: current, title: titles[current] || prettySlug(current), showings: [] });
        }
      } else if (current !== null) {
        const [sid, utc, tm, ampm] = data;
        movies.get(current).showings.push(
          { showtimeId: Number(sid), showDateTimeUtc: utc, time: `${tm} ${ampm}`, format: currentFmt });
      }
    }
    return { theatreSlug: slug, movies: [...movies.values()] };
  }

  function mergeShowtimes(listings) {
    const movies = new Map();
    for (const l of listings) {
      for (const mv of l.movies) {
        if (!movies.has(mv.slug)) movies.set(mv.slug, { slug: mv.slug, title: mv.title, showings: [] });
        movies.get(mv.slug).showings.push(...mv.showings);
      }
    }
    for (const m of movies.values()) {
      const seen = new Set();
      m.showings = m.showings
        .slice().sort((a, b) => (a.showDateTimeUtc < b.showDateTimeUtc ? -1 : a.showDateTimeUtc > b.showDateTimeUtc ? 1 : 0))
        .filter((s) => !seen.has(s.showtimeId) && seen.add(s.showtimeId));
    }
    return [...movies.values()];
  }

  function addDays(iso, n) {
    const d = new Date(iso + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  async function run() {
    const m = location.pathname.match(/\/movie-theatres\/([a-z0-9-]+\/[a-z0-9-]+)/i);
    if (!/amctheatres\.com$/.test(location.hostname) || !m) {
      alert("Open your theatre's page on amctheatres.com first, then tap this bookmark.");
      return;
    }
    const path = m[1];
    const now = new Date();
    const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const start = new URLSearchParams(location.search).get("date") || today;
    const answer = prompt(`Seat Scanner: how many days of showtimes from ${start}?`, "1");
    if (answer === null) return;
    const days = Math.min(14, Math.max(1, parseInt(answer, 10) || 1));

    const listings = [];
    for (let i = 0; i < days; i++) {
      const day = addDays(start, i);
      document.title = `Seat Scanner: reading ${i + 1}/${days}…`;
      const res = await fetch(`/movie-theatres/${path}/showtimes?date=${day}`, { credentials: "include" });
      if (!res.ok) throw new Error(`AMC returned ${res.status} for ${day}`);
      listings.push(parseShowtimes(await res.text(), path));
    }
    const listing = {
      theatre: path,
      theatreSlug: theatreSlug(path),
      date: start,
      days,
      movies: mergeShowtimes(listings),
      fetchedAtUtc: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    };
    location.href = "__APP_URL__#import=" + encodeURIComponent(JSON.stringify(listing));
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseShowtimes, mergeShowtimes };   // node tests
  } else {
    run().catch((e) => alert("Seat Scanner couldn't read showtimes: " + e.message));
  }
})();
