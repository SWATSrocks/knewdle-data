/**
 * Knewdle NOW ratings: a tiny free Cloudflare Worker.
 *
 *   POST /rate      {"shop": "...", "stars": 1-5, "device": "..."}   -> {"ok": true}
 *   GET  /averages  -> {"generated": "...", "shops": [{"k": shop, "avg": 4.6, "n": 12}, ...]}  (3+ ratings only)
 *   POST /report    {"shop": "...", "device": "...", "kind": "closed" | "closed-here" | "open-here", "name": "Shop name"} -> {"ok": true}
 *                   ("closed-here": the phone passed the in-person check, the same one as Passport stamps)
 *                   "open-here": the phone was at the shop and found it open (arrival check or Passport stamp)
 *   GET  /closed    -> {"shops": [{"k": shop, "name": "...", "n": 2, "here": 1, "last": "date"}, ...]}
 *                   (last 365 days: 1 phone that was at the shop, or 2+ phones from anywhere; never a shop
 *                   someone found open in person since the last "closed" report)
 *   GET  /closed?all=1 -> same, but every shop with any "closed" report, plus "open": [{"k", "last"}]
 *   Leaderboards (opt-in, nickname only; never which shops or when, only totals):
 *   POST /lb/join   {"device", "nick"}            -> {"ok": true, "nick"} (3-16 letters/numbers, family-friendly, unique)
 *   POST /lb/sync   {"device", "stamps": [{"k": shop, "d": ["2026-10-21", ...]}]} -> {"ok": true, "me": {...}}
 *                   (the phone's Passport visit dates; the Worker keeps only the totals it computes from them)
 *   POST /lb/leave  {"device"}                    -> {"ok": true}  (removes the nickname and totals)
 *   GET  /lb        -> {"month": "2026-10", "boards": {"month": [{"nick","n"}], "all": [...], "streak": [...]},
 *                       "shops": [{"k", "name", "n"}]}   (shops: most in-person visitors this month)
 *   GET  /lb/me?device=... -> {"joined": true, "nick", "month_shops", "all_shops", "best_streak", "rank": {...}}
 *   Install alerts (for the developer's private "Knewdle Pulse" widget):
 *   POST /install   {"device", "src", "medium"} -> {"ok": true}   (sent once, the first time the app opens;
 *                   src/medium = the Play install link's utm tags, e.g. "instagram"/"qr"; nothing about the person)
 *   GET  /pulse?key=PULSE_KEY -> {"total", "today", "day", "last", "sources_today", "recent"}  (404 without the key)
 *   GET  /pulse/origin?key=PULSE_KEY -> {"stores": {"Google Play": n, …}, "copies": [...], "copies_total"}: which store installed
 *                   each copy, and copies that aren't the real app (another package name, or a signing key no Play install has).
 *   GET  /pulse/map?key=PULSE_KEY&days=7 -> {"installs": [{city, region, lat, lon, n, src}], "earlier": [{lat, lon, n}]}
 *                   installs: by the city Cloudflare places each install's network in (city level, never the address);
 *                   in_range / unplaced: all installs in the range, and how many had no city (unplaced_by_country);
 *                   earlier: phones from before city tracking, placed at the first shop they rated or reported
 *                   (rounded to ~10 km). Developer-only: 404 without the key.
 *   A new install is also pushed to the private ntfy.sh topic derived from PULSE_KEY, which only the widget knows.
 *   GET  /          -> short description
 *
 * Stars only (no text), one rating per phone per shop (changing it replaces the old one).
 * The app only offers rating after a Passport stamp, i.e. after an in-person visit.
 */
const MIN_RATINGS = 3;           // an average is published only with at least this many ratings
const MIN_CLOSED_REPORTS = 2;    // different phones that must say "closed" before a shop is hidden for everyone
const MIN_HERE_REPORTS = 1;      // ...or this many phones that said so while standing at the shop
const KINDS = ["closed", "closed-here", "open-here"]; // open-here: found it open in person (arrival check or stamp)
const REPORT_DAYS = 365;         // reports older than this no longer count
const PER_DEVICE_PER_DAY = 25;   // a phone can rate (or change) at most this many shops a day
const PER_ADDRESS_PER_DAY = 200; // one network address (e.g. a café's Wi-Fi) at most this many a day

const SHOP = /^[a-z0-9.\-]{1,80}@-?\d{1,3}\.\d{2},-?\d{1,3}\.\d{2}$/;
const DEVICE = /^[A-Za-z0-9\-]{16,64}$/;

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*", ...extra },
  });
}

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 24);
}

async function rate(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "bad request" }, 400);
  }
  const shop = String(body.shop || "").toLowerCase();
  const device = String(body.device || "");
  const stars = Number(body.stars);
  if (!SHOP.test(shop) || !DEVICE.test(device) || !Number.isInteger(stars) || stars < 1 || stars > 5) {
    return json({ ok: false, error: "bad rating" }, 400);
  }
  const day = new Date().toISOString().slice(0, 10);

  // Limits: per network address (hashed, never stored raw) and per phone.
  const who = await sha256((request.headers.get("cf-connecting-ip") || "unknown") + "|" + day);
  const row = await env.DB.prepare("SELECT count FROM limits WHERE who = ? AND day = ?").bind(who, day).first();
  if (row && row.count >= PER_ADDRESS_PER_DAY) return json({ ok: false, error: "too many today" }, 429);
  const mine = await env.DB.prepare("SELECT COUNT(*) AS c FROM ratings WHERE device = ? AND updated = ?")
    .bind(device, day).first();
  if (mine && mine.c >= PER_DEVICE_PER_DAY) return json({ ok: false, error: "too many today" }, 429);

  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO ratings (shop, device, stars, updated) VALUES (?, ?, ?, ?) " +
        "ON CONFLICT (shop, device) DO UPDATE SET stars = excluded.stars, updated = excluded.updated"
    ).bind(shop, device, stars, day),
    env.DB.prepare(
      "INSERT INTO limits (who, day, count) VALUES (?, ?, 1) " +
        "ON CONFLICT (who, day) DO UPDATE SET count = count + 1"
    ).bind(who, day),
    // Old limit rows are useless after a day.
    env.DB.prepare("DELETE FROM limits WHERE day < ?").bind(day),
  ]);
  return json({ ok: true });
}

async function report(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "bad request" }, 400);
  }
  const shop = String(body.shop || "").toLowerCase();
  const device = String(body.device || "");
  const kind = String(body.kind || "closed");
  const name = String(body.name || "").slice(0, 80).replace(/[<>\u0000-\u001f]/g, " ");
  if (!SHOP.test(shop) || !DEVICE.test(device) || !KINDS.includes(kind)) {
    return json({ ok: false, error: "bad report" }, 400);
  }
  const day = new Date().toISOString().slice(0, 10);
  const who = await sha256((request.headers.get("cf-connecting-ip") || "unknown") + "|" + day);
  const row = await env.DB.prepare("SELECT count FROM limits WHERE who = ? AND day = ?").bind(who, day).first();
  if (row && row.count >= PER_ADDRESS_PER_DAY) return json({ ok: false, error: "too many today" }, 429);
  const mine = await env.DB.prepare("SELECT COUNT(*) AS c FROM reports WHERE device = ? AND updated = ?")
    .bind(device, day).first();
  if (mine && mine.c >= 10) return json({ ok: false, error: "too many today" }, 429);
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO reports (shop, device, kind, name, updated) VALUES (?, ?, ?, ?, ?) " +
        "ON CONFLICT (shop, device, kind) DO UPDATE SET updated = excluded.updated, name = excluded.name"
    ).bind(shop, device, kind, name, day),
    env.DB.prepare(
      "INSERT INTO limits (who, day, count) VALUES (?, ?, 1) ON CONFLICT (who, day) DO UPDATE SET count = count + 1"
    ).bind(who, day),
  ]);
  return json({ ok: true });
}

async function unreport(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "bad request" }, 400);
  }
  const shop = String(body.shop || "").toLowerCase();
  const device = String(body.device || "");
  if (!SHOP.test(shop) || !DEVICE.test(device)) return json({ ok: false, error: "bad report" }, 400);
  await env.DB.prepare("DELETE FROM reports WHERE shop = ? AND device = ?").bind(shop, device).run();
  return json({ ok: true });
}

async function closed(env, all) {
  // Per shop: phones that said "closed" (anywhere / at the shop) and the latest in-person "it's open".
  // A shop someone found open in person on or after the last "closed" report is not listed.
  const since = new Date(Date.now() - REPORT_DAYS * 86400000).toISOString().slice(0, 10);
  const { results } = await env.DB.prepare(
    "SELECT shop AS k, MAX(name) AS name, " +
      "COUNT(DISTINCT CASE WHEN kind IN ('closed', 'closed-here') THEN device END) AS n, " +
      "COUNT(DISTINCT CASE WHEN kind = 'closed-here' THEN device END) AS here, " +
      "MAX(CASE WHEN kind IN ('closed', 'closed-here') THEN updated END) AS last_closed, " +
      "MAX(CASE WHEN kind = 'open-here' THEN updated END) AS last_open " +
      "FROM reports WHERE updated >= ? GROUP BY shop"
  ).bind(since).all();
  const shops = [];
  const open = [];
  for (const r of results) {
    if (r.last_open) open.push({ k: r.k, last: r.last_open });
    if (!r.n || (r.last_open && r.last_open >= r.last_closed)) continue;
    if (all || r.n >= MIN_CLOSED_REPORTS || r.here >= MIN_HERE_REPORTS) {
      shops.push({ k: r.k, name: r.name, n: r.n, here: r.here, last: r.last_closed });
    }
  }
  const body = { generated: new Date().toISOString(), min: MIN_CLOSED_REPORTS, min_here: MIN_HERE_REPORTS, shops };
  if (all) body.open = open;
  return json(body, 200, { "cache-control": "public, max-age=900" });
}

async function averages(env) {
  const { results } = await env.DB.prepare(
    "SELECT shop AS k, ROUND(AVG(stars), 1) AS avg, COUNT(*) AS n FROM ratings GROUP BY shop HAVING COUNT(*) >= ?"
  ).bind(MIN_RATINGS).all();
  return json({ generated: new Date().toISOString(), min: MIN_RATINGS, shops: results },
    200, { "cache-control": "public, max-age=900" });
}

// ---------------------------------------------------------------------------------------------
// Leaderboards
// ---------------------------------------------------------------------------------------------
const NICK = /^[A-Za-z0-9][A-Za-z0-9 _.\-]{1,14}[A-Za-z0-9]$/;   // 3-16 characters
const LB_SINCE = "2026-09-01";        // stamps before the Passport existed don't count
const LB_MAX_SHOPS_PER_DAY = 6;       // more distinct shops than this in one day: only the first 6 count
const LB_MAX_DAY_SPREAD_KM = 300;     // shops farther apart than this on the same day: that day doesn't count
const LB_TOP = 25;
const LB_SYNCS_PER_DAY = 40;
// Words a nickname may not contain (checked after undoing look-alike letters and repeats: "fuuuck", "sh1t").
const BLOCKED = ["fuck", "fuk", "shit", "bitch", "cunt", "dick", "cock", "pussy", "asshole", "whore", "slut", "nigg",
  "niga", "fag", "retard", "rapist", "porn", "penis", "vagina", "nazi", "hitler", "kike", "chink", "tranny", "dyke",
  "twat", "wank", "jizz", "molest", "pedo", "boob", "tits", "kkk", "heil", "admin", "moderator", "knewdle", "official",
  "swatsrocks", "support"];

function nickKey(nick) {
  return nick.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function nickBlocked(nick) {
  const leet = { "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "@": "a", "$": "s", "!": "i" };
  const plain = nick.toLowerCase().split("").map((c) => leet[c] ?? c).join("").replace(/[^a-z]/g, "");
  const squeezed = plain.replace(/(.)\1+/g, "$1");   // "fuuuck" -> "fuck"
  return BLOCKED.some((w) => plain.includes(w) || (!/(.)\1/.test(w) && squeezed.includes(w)));
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

async function body(request) {
  try {
    return await request.json();
  } catch {
    return null;
  }
}

function km(a, b) {
  const r = Math.PI / 180, dLat = (b[0] - a[0]) * r, dLon = (b[1] - a[1]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLon / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

/** Totals from a Passport's visit dates, after dropping anything implausible. */
function lbTotals(stamps, now = new Date()) {
  const maxDay = new Date(now.getTime() + 86400000).toISOString().slice(0, 10);
  const month = now.toISOString().slice(0, 7);
  const byDay = new Map();   // day -> [[shop, [lat, lon]]]
  for (const s of Array.isArray(stamps) ? stamps.slice(0, 1000) : []) {
    const k = String(s && s.k || "").toLowerCase();
    if (!SHOP.test(k)) continue;
    const at = k.split("@")[1].split(",").map(Number);
    const days = new Set((Array.isArray(s.d) ? s.d.slice(0, 400) : []).map(String)
      .filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d) && d >= LB_SINCE && d <= maxDay));
    for (const d of days) {
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push([k, at]);
    }
  }
  const all = new Set(), inMonth = new Set(), weeks = new Set();
  for (const [d, list] of byDay) {
    const shops = [...new Map(list).entries()].sort().slice(0, LB_MAX_SHOPS_PER_DAY);
    let spread = 0;
    for (let i = 0; i < shops.length; i++) for (let j = i + 1; j < shops.length; j++) spread = Math.max(spread, km(shops[i][1], shops[j][1]));
    if (spread > LB_MAX_DAY_SPREAD_KM) continue;
    for (const [k] of shops) {
      all.add(k);
      if (d.startsWith(month)) inMonth.add(k);
    }
    weeks.add(Math.floor((Date.parse(d + "T00:00:00Z") / 86400000 + 3) / 7));
  }
  const ws = [...weeks].sort((a, b) => a - b);
  let best = 0, run = 0;
  for (let i = 0; i < ws.length; i++) {
    run = i > 0 && ws[i] === ws[i - 1] + 1 ? run + 1 : 1;
    best = Math.max(best, run);
  }
  return { month, month_shops: inMonth.size, all_shops: all.size, best_streak: best };
}

async function lbJoin(request, env) {
  const b = await body(request);
  const device = String(b && b.device || "");
  const nick = String(b && b.nick || "").trim().replace(/\s+/g, " ");
  if (!DEVICE.test(device)) return json({ ok: false, error: "bad request" }, 400);
  if (!NICK.test(nick) || nickKey(nick).length < 3) return json({ ok: false, error: "nick-format" }, 400);
  if (nickBlocked(nick)) return json({ ok: false, error: "nick-blocked" }, 400);
  const key = nickKey(nick);
  const taken = await env.DB.prepare("SELECT device FROM lb_players WHERE nick_norm = ?").bind(key).first();
  if (taken && taken.device !== device) return json({ ok: false, error: "nick-taken" }, 409);
  await env.DB.prepare(
    "INSERT INTO lb_players (device, nick, nick_norm, joined) VALUES (?, ?, ?, ?) " +
      "ON CONFLICT (device) DO UPDATE SET nick = excluded.nick, nick_norm = excluded.nick_norm"
  ).bind(device, nick, key, today()).run();
  return json({ ok: true, nick });
}

async function lbSync(request, env) {
  const b = await body(request);
  const device = String(b && b.device || "");
  if (!DEVICE.test(device)) return json({ ok: false, error: "bad request" }, 400);
  const me = await env.DB.prepare("SELECT device FROM lb_players WHERE device = ?").bind(device).first();
  if (!me) return json({ ok: false, error: "not-joined" }, 404);
  const day = today();
  const who = "lb:" + device;
  const lim = await env.DB.prepare("SELECT count FROM limits WHERE who = ? AND day = ?").bind(who, day).first();
  if (lim && lim.count >= LB_SYNCS_PER_DAY) return json({ ok: false, error: "too many today" }, 429);
  const t = lbTotals(b.stamps);
  await env.DB.batch([
    env.DB.prepare("UPDATE lb_players SET month = ?, month_shops = ?, all_shops = ?, best_streak = ?, updated = ? WHERE device = ?")
      .bind(t.month, t.month_shops, t.all_shops, t.best_streak, day, device),
    env.DB.prepare("INSERT INTO limits (who, day, count) VALUES (?, ?, 1) ON CONFLICT (who, day) DO UPDATE SET count = count + 1")
      .bind(who, day),
  ]);
  return json({ ok: true, me: await lbMeRow(env, device) });
}

async function lbLeave(request, env) {
  const b = await body(request);
  const device = String(b && b.device || "");
  if (!DEVICE.test(device)) return json({ ok: false, error: "bad request" }, 400);
  await env.DB.prepare("DELETE FROM lb_players WHERE device = ?").bind(device).run();
  return json({ ok: true });
}

async function lbMeRow(env, device) {
  const month = new Date().toISOString().slice(0, 7);
  const p = await env.DB.prepare("SELECT nick, hidden, month, month_shops, all_shops, best_streak FROM lb_players WHERE device = ?")
    .bind(device).first();
  if (!p) return { joined: false };
  const ms = p.month === month ? p.month_shops : 0;
  return {
    joined: true, nick: p.nick, hidden: !!p.hidden, month, month_shops: ms, all_shops: p.all_shops, best_streak: p.best_streak,
    rank: {
      month: ms > 0 ? 1 + (await env.DB.prepare("SELECT COUNT(*) AS c FROM lb_players WHERE hidden = 0 AND month = ? AND month_shops > ?").bind(month, ms).first()).c : null,
      all: p.all_shops > 0 ? 1 + (await env.DB.prepare("SELECT COUNT(*) AS c FROM lb_players WHERE hidden = 0 AND all_shops > ?").bind(p.all_shops).first()).c : null,
      streak: p.best_streak > 0 ? 1 + (await env.DB.prepare("SELECT COUNT(*) AS c FROM lb_players WHERE hidden = 0 AND best_streak > ?").bind(p.best_streak).first()).c : null,
    },
  };
}

async function lbBoards(env) {
  const month = new Date().toISOString().slice(0, 7);
  const q = (sql, ...args) => env.DB.prepare(sql).bind(...args).all().then((r) => r.results);
  const [m, a, s, shops] = await Promise.all([
    q("SELECT nick, month_shops AS n FROM lb_players WHERE hidden = 0 AND month = ? AND month_shops > 0 " +
      "ORDER BY month_shops DESC, all_shops DESC, joined ASC LIMIT ?", month, LB_TOP),
    q("SELECT nick, all_shops AS n FROM lb_players WHERE hidden = 0 AND all_shops > 0 ORDER BY all_shops DESC, joined ASC LIMIT ?", LB_TOP),
    q("SELECT nick, best_streak AS n FROM lb_players WHERE hidden = 0 AND best_streak > 0 ORDER BY best_streak DESC, joined ASC LIMIT ?", LB_TOP),
    q("SELECT shop AS k, MAX(name) AS name, COUNT(DISTINCT device) AS n FROM reports WHERE kind = 'open-here' AND updated >= ? " +
      "GROUP BY shop ORDER BY n DESC, MAX(updated) DESC LIMIT 20", month + "-01"),
  ]);
  return json({ generated: new Date().toISOString(), month, boards: { month: m, all: a, streak: s }, shops },
    200, { "cache-control": "public, max-age=300" });
}


// ---------- install alerts (developer only) ----------
const INSTALLS_PER_ADDRESS_PER_DAY = 5;
function etDay(d = new Date()) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(d); // 2026-10-29
}
function tag(v) {
  return String(v || "").toLowerCase().replace(/[^a-z0-9_.\-]/g, "").slice(0, 32);
}
async function pulseTopic(key) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("knewdle-pulse:" + key));
  return "kn-" + [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}
function sameKey(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let x = 0; for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return x === 0;
}
// ---- Where each copy of the app came from (catches copies on other stores and repackaged clones) ----
const OUR_PKG = "com.swatsrocks.knewdle";
const STORES = {
  "com.android.vending": "Google Play", "com.sec.android.app.samsungapps": "Galaxy Store", "com.amazon.venezia": "Amazon Appstore",
  "com.huawei.appmarket": "Huawei AppGallery", "cm.aptoide.pt": "Aptoide", "com.apkpure.aegon": "APKPure", "com.uptodown": "Uptodown",
  "com.xiaomi.market": "Xiaomi GetApps", "com.xiaomi.mipicks": "Xiaomi GetApps", "com.heytap.market": "OPPO App Market",
  "com.oppo.market": "OPPO App Market", "com.bbk.appstore": "vivo App Store", "com.vivo.appstore": "vivo App Store",
  "com.android.shell": "Installed from a computer", "com.google.android.packageinstaller": "APK file (sideloaded)",
  "com.android.packageinstaller": "APK file (sideloaded)", "none": "Unknown / preinstalled",
};
function storeName(installer) {
  if (!installer) return null;
  return STORES[installer] || installer;
}
function pkgName(v) { return String(v || "").replace(/[^A-Za-z0-9_.]/g, "").slice(0, 100) || null; }
function sigHex(v) { const h = String(v || "").toLowerCase().replace(/[^0-9a-f]/g, "").slice(0, 64); return h.length >= 16 ? h : null; }
// The signatures Google Play installs carry are the real app; anything else (or another package name) is suspect.
async function trustedSigs(env) {
  const r = await env.DB.prepare("SELECT DISTINCT sig FROM installs WHERE installer = 'com.android.vending' AND pkg = ? AND sig IS NOT NULL")
    .bind(OUR_PKG).all();
  return new Set((r.results || []).map((x) => x.sig));
}
function suspectWhy(row, trusted) {
  if (row.pkg && row.pkg !== OUR_PKG) return "renamed copy (" + row.pkg + ")";
  if (row.sig && trusted.size && !trusted.has(row.sig)) return "signed with a different key";
  return null;
}
async function originStats(env) {
  const [stores, rows, trusted] = await Promise.all([
    env.DB.prepare("SELECT COALESCE(installer, '') AS i, COUNT(*) AS n FROM installs WHERE device NOT LIKE 'smoke-test-device%' GROUP BY i ORDER BY n DESC").all(),
    env.DB.prepare("SELECT ts, installer, pkg, sig, city, region, country FROM installs WHERE (sig IS NOT NULL OR pkg IS NOT NULL) AND device NOT LIKE 'smoke-test-device%' ORDER BY ts DESC LIMIT 2000").all(),
    trustedSigs(env),
  ]);
  const byStore = {};
  let unknown = 0;
  for (const r of stores.results || []) {
    if (!r.i) { unknown += r.n; continue; }
    const name = storeName(r.i); byStore[name] = (byStore[name] || 0) + r.n;
  }
  const copies = [];
  for (const r of rows.results || []) {
    const why = suspectWhy(r, trusted);
    if (why) copies.push({ ts: r.ts, store: storeName(r.installer) || "unknown", why, sig: (r.sig || "").slice(0, 12),
                           where: r.city ? r.city + (r.region ? ", " + r.region : "") : (r.country || null) });
  }
  return { stores: byStore, not_reported: unknown, copies: copies.slice(0, 20), copies_total: copies.length,
           trusted_keys: [...trusted].map((s) => s.slice(0, 12)) };
}

async function pulseStats(env) {
  const day = etDay();
  const [tot, td, last, srcs, recent] = await Promise.all([
    env.DB.prepare("SELECT COUNT(*) AS n FROM installs").first(),
    env.DB.prepare("SELECT COUNT(*) AS n FROM installs WHERE day = ?").bind(day).first(),
    env.DB.prepare("SELECT ts, src, medium FROM installs ORDER BY ts DESC LIMIT 1").first(),
    env.DB.prepare("SELECT src, COUNT(*) AS n FROM installs WHERE day = ? GROUP BY src ORDER BY n DESC").bind(day).all(),
    env.DB.prepare("SELECT ts, src, medium, installer FROM installs ORDER BY ts DESC LIMIT 20").all(),
  ]);
  const rec = (recent.results || []).map((r) => ({ ts: r.ts, src: r.src, medium: r.medium, store: storeName(r.installer) }));
  return { total: tot ? tot.n : 0, today: td ? td.n : 0, day, last: last || null,
           sources_today: srcs.results || [], recent: rec };
}
async function install(request, env) {
  const b = await body(request);
  const device = String(b && b.device || "");
  if (!DEVICE.test(device)) return json({ ok: false, error: "bad request" }, 400);
  const src = tag(b.src) || "unknown", medium = tag(b.medium) || "unknown";
  // Which store installed this copy, its package name and its signing-key fingerprint (sent by 1.16.4+).
  const installer = b.installer === undefined ? null : (pkgName(b.installer) || "none");
  const pkg = pkgName(b.pkg), sig = sigHex(b.sig);
  const who = "in:" + await sha256(request.headers.get("cf-connecting-ip") || "?");
  const day = today();
  const lim = await env.DB.prepare("SELECT count FROM limits WHERE who = ? AND day = ?").bind(who, day).first();
  if (lim && lim.count >= INSTALLS_PER_ADDRESS_PER_DAY) return json({ ok: true });
  // City-level place from Cloudflare's network lookup (no GPS, never the address itself).
  const cf = request.cf || {};
  // Any country (a phone abroad, on a VPN or a carrier that routes through another country still counts).
  const geoOk = Number.isFinite(Number(cf.latitude)) && Number.isFinite(Number(cf.longitude));
  const country = String(cf.country || "").slice(0, 2) || null;
  const city = geoOk ? String(cf.city || "").slice(0, 60) || null : null;
  const region = geoOk ? String((country === "US" ? cf.regionCode : country) || cf.region || "").slice(0, 40) || null : null;
  const lat = geoOk ? Math.round(Number(cf.latitude) * 100) / 100 : null;
  const lon = geoOk ? Math.round(Number(cf.longitude) * 100) / 100 : null;
  const [r] = await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO installs (device, src, medium, day, ts, city, region, lat, lon, country, installer, pkg, sig) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
      .bind(device, src, medium, etDay(), Date.now(), city, region, lat, lon, country, installer, pkg, sig),
    env.DB.prepare("INSERT INTO limits (who, day, count) VALUES (?, ?, 1) ON CONFLICT (who, day) DO UPDATE SET count = count + 1")
      .bind(who, day),
  ]);
  const isNew = r && r.meta && r.meta.changes > 0;
  if (!isNew && (installer || pkg || sig)) {
    // A phone that installed before 1.16.4 reporting where it came from, once, after updating.
    await env.DB.prepare("UPDATE installs SET installer = COALESCE(installer, ?), pkg = COALESCE(pkg, ?), sig = COALESCE(sig, ?) WHERE device = ?")
      .bind(installer, pkg, sig, device).run();
  }
  let suspect = null;
  if ((pkg || sig) && !device.startsWith("smoke-test-device")) {
    try { suspect = suspectWhy({ pkg, sig }, await trustedSigs(env)); } catch (e) { suspect = null; }
  }
  if ((isNew || suspect) && env.PULSE_KEY && !device.startsWith("smoke-test-device")) {
    try {
      const st = await pulseStats(env);
      const msg = JSON.stringify({ kind: isNew ? "install" : "origin", total: st.total, today: st.today, src, medium, ts: Date.now(),
                                   city: city ? city + (region ? ", " + region : "") : null,
                                   store: storeName(installer), suspect });
      await fetch("https://ntfy.sh/" + await pulseTopic(env.PULSE_KEY), { method: "POST", body: msg });
    } catch (e) { /* the widget also checks /pulse every 15 minutes */ }
  }
  return json({ ok: true });
}

// Where installs come from, for the private Pulse map.
function shopPoint(shop) {
  const m = /@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/.exec(String(shop || ""));
  if (!m) return null;
  const lat = Number(m[1]), lon = Number(m[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || (Math.abs(lat) < 0.5 && Math.abs(lon) < 0.5)) return null;
  return [Math.round(lat * 10) / 10, Math.round(lon * 10) / 10];
}
async function pulseMap(env, days) {
  const since = days > 0 ? Date.now() - days * 86400000 : 0;
  const [inst, all, rated, reported, unplaced] = await Promise.all([
    env.DB.prepare("SELECT city, region, lat, lon, src, medium FROM installs WHERE lat IS NOT NULL AND ts >= ? AND device NOT LIKE 'smoke-test-device%'")
      .bind(since).all(),
    env.DB.prepare("SELECT device, lat, ts FROM installs").all(),
    env.DB.prepare("SELECT device, shop, updated FROM ratings WHERE device NOT LIKE 'smoke-test-device%'").all(),
    env.DB.prepare("SELECT device, shop, updated FROM reports WHERE device NOT LIKE 'smoke-test-device%'").all(),
    // Installs since city tracking began (the first one with a city) that still have none.
    env.DB.prepare("SELECT COALESCE(country, '') AS c, COUNT(*) AS n FROM installs WHERE lat IS NULL AND ts >= ? " +
      "AND ts >= (SELECT COALESCE(MIN(ts), 9e15) FROM installs WHERE lat IS NOT NULL) AND device NOT LIKE 'smoke-test-device%' GROUP BY c")
      .bind(since).all(),
  ]);
  const noPlace = {};
  for (const r of unplaced.results || []) noPlace[r.c || "unknown"] = r.n;
  const byCity = new Map();
  for (const r of inst.results || []) {
    const k = (r.city || "?") + "|" + (r.region || "") ;
    const c = byCity.get(k) || { city: r.city, region: r.region, lat: 0, lon: 0, n: 0, src: {} };
    c.lat += r.lat; c.lon += r.lon; c.n += 1;
    const label = r.src === "google-play" && r.medium === "organic" ? "play-search" : (r.src || "unknown");
    c.src[label] = (c.src[label] || 0) + 1;
    byCity.set(k, c);
  }
  const installs = [...byCity.values()].map((c) => ({ ...c, lat: Math.round(c.lat / c.n * 100) / 100, lon: Math.round(c.lon / c.n * 100) / 100 }))
    .sort((a, b) => b.n - a.n);
  // Earlier phones: no city on file, so use the first shop they rated or reported (they were there in person).
  const located = new Set((all.results || []).filter((r) => r.lat !== null).map((r) => r.device));
  const first = new Map();
  for (const r of [...(rated.results || []), ...(reported.results || [])]) {
    if (located.has(r.device)) continue;
    const p = shopPoint(r.shop);
    if (!p) continue;
    const f = first.get(r.device);
    if (!f || r.updated < f.updated) first.set(r.device, { updated: r.updated, p });
  }
  const byArea = new Map();
  for (const { p } of first.values()) {
    const k = p.join(",");
    byArea.set(k, (byArea.get(k) || 0) + 1);
  }
  const earlier = [...byArea.entries()].map(([k, n]) => { const [lat, lon] = k.split(",").map(Number); return { lat, lon, n }; })
    .sort((a, b) => b.n - a.n);
  const placedN = installs.reduce((t, c) => t + c.n, 0);
  const unplacedN = Object.values(noPlace).reduce((t, n) => t + n, 0);
  return { days, installs, earlier, installs_total: placedN, in_range: placedN + unplacedN, unplaced: unplacedN,
           unplaced_by_country: noPlace,
           earlier_total: earlier.reduce((t, c) => t + c.n, 0) };
}


export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST",
        "access-control-allow-headers": "content-type" } });
    }
    try {
      if (url.pathname === "/rate" && request.method === "POST") return await rate(request, env);
      if (url.pathname === "/averages" && request.method === "GET") return await averages(env);
      if (url.pathname === "/report" && request.method === "POST") return await report(request, env);
      if (url.pathname === "/unreport" && request.method === "POST") return await unreport(request, env);
      if (url.pathname === "/closed" && request.method === "GET") return await closed(env, url.searchParams.get("all") === "1");
      if (url.pathname === "/lb" && request.method === "GET") return await lbBoards(env);
      if (url.pathname === "/lb/me" && request.method === "GET") {
        const device = String(url.searchParams.get("device") || "");
        if (!DEVICE.test(device)) return json({ ok: false, error: "bad request" }, 400);
        return json({ ok: true, me: await lbMeRow(env, device) });
      }
      if (url.pathname === "/lb/join" && request.method === "POST") return await lbJoin(request, env);
      if (url.pathname === "/lb/sync" && request.method === "POST") return await lbSync(request, env);
      if (url.pathname === "/lb/leave" && request.method === "POST") return await lbLeave(request, env);
      if (url.pathname === "/install" && request.method === "POST") return await install(request, env);
      if (url.pathname === "/pulse/map" && request.method === "GET") {
        if (!env.PULSE_KEY || !sameKey(String(url.searchParams.get("key") || ""), env.PULSE_KEY)) return json({ ok: false, error: "not found" }, 404);
        const days = Math.max(0, Math.min(3650, parseInt(url.searchParams.get("days") || "0", 10) || 0));
        return json({ ok: true, ...(await pulseMap(env, days)) }, 200, { "cache-control": "no-store" });
      }
      if (url.pathname === "/pulse/origin" && request.method === "GET") {
        if (!env.PULSE_KEY || !sameKey(String(url.searchParams.get("key") || ""), env.PULSE_KEY)) return json({ ok: false, error: "not found" }, 404);
        return json({ ok: true, ...(await originStats(env)) }, 200, { "cache-control": "no-store" });
      }
      if (url.pathname === "/pulse" && request.method === "GET") {
        if (!env.PULSE_KEY || !sameKey(String(url.searchParams.get("key") || ""), env.PULSE_KEY)) return json({ ok: false, error: "not found" }, 404);
        return json({ ok: true, ...(await pulseStats(env)) }, 200, { "cache-control": "no-store" });
      }
      if (url.pathname === "/") return json({ name: "Knewdle NOW ratings", info: "https://swatsrocks.github.io/knewdle-data/" });
      return json({ ok: false, error: "not found" }, 404);
    } catch (e) {
      return json({ ok: false, error: "server error" }, 500);
    }
  },
};
