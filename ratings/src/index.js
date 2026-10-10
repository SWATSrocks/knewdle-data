/**
 * Knewdle NOW ratings: a tiny free Cloudflare Worker.
 *
 *   POST /rate      {"shop": "...", "stars": 1-5, "device": "..."}   -> {"ok": true}
 *   GET  /averages  -> {"generated": "...", "shops": [{"k": shop, "avg": 4.6, "n": 12}, ...]}  (3+ ratings only)
 *   POST /report    {"shop": "...", "device": "...", "kind": "closed", "name": "Shop name"} -> {"ok": true}
 *   GET  /closed    -> {"shops": [{"k": shop, "name": "...", "n": 2}, ...]}  (2+ phones, last 365 days)
 *   GET  /          -> short description
 *
 * Stars only (no text), one rating per phone per shop (changing it replaces the old one).
 * The app only offers rating after a Passport stamp, i.e. after an in-person visit.
 */
const MIN_RATINGS = 3;
const MIN_CLOSED_REPORTS = 2;    // different phones that must say "closed" before a shop is hidden for everyone
const REPORT_DAYS = 365;         // reports older than this no longer count           // an average is published only with at least this many ratings
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
  if (!SHOP.test(shop) || !DEVICE.test(device) || kind !== "closed") {
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

async function closed(env) {
  const since = new Date(Date.now() - REPORT_DAYS * 86400000).toISOString().slice(0, 10);
  const { results } = await env.DB.prepare(
    "SELECT shop AS k, MAX(name) AS name, COUNT(*) AS n FROM reports WHERE kind = 'closed' AND updated >= ? " +
      "GROUP BY shop HAVING COUNT(*) >= ?"
  ).bind(since, MIN_CLOSED_REPORTS).all();
  return json({ generated: new Date().toISOString(), min: MIN_CLOSED_REPORTS, shops: results },
    200, { "cache-control": "public, max-age=900" });
}

async function averages(env) {
  const { results } = await env.DB.prepare(
    "SELECT shop AS k, ROUND(AVG(stars), 1) AS avg, COUNT(*) AS n FROM ratings GROUP BY shop HAVING COUNT(*) >= ?"
  ).bind(MIN_RATINGS).all();
  return json({ generated: new Date().toISOString(), min: MIN_RATINGS, shops: results },
    200, { "cache-control": "public, max-age=900" });
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
      if (url.pathname === "/closed" && request.method === "GET") return await closed(env);
      if (url.pathname === "/") return json({ name: "Knewdle NOW ratings", info: "https://swatsrocks.github.io/knewdle-data/" });
      return json({ ok: false, error: "not found" }, 404);
    } catch (e) {
      return json({ ok: false, error: "server error" }, 500);
    }
  },
};
