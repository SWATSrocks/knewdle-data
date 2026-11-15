// Knewdle NOW Featured: shops pay monthly (PayPal Subscriptions) to be pinned in green at the top of the app's list.
//
// How it stays hands-off:
//  * PayPal holds the plans: 3 tiers x (regular, founding = half price for the first FOUNDING_SPOTS shops, kept as
//    long as they stay subscribed). They're created once, automatically, by POST /pulse/paypal/setup.
//  * The "Get featured" page subscribes with PayPal's own button, then tells us the subscription ID (/featured/start).
//  * PayPal's webhooks (and an hourly re-check) only tell us WHICH subscription changed: we always read its real state
//    back from PayPal before acting, so a forged webhook can't feature or un-feature anyone.
//  * A shop is featured while its subscription is ACTIVE, or CANCELLED but still inside the month it paid for, and
//    not paused by the developer. Phones read the list from /overrides ("featured") within a couple of minutes.
//  * Every new subscriber, payment, failed payment and cancellation is pushed to Knewdle Pulse.
//
// Sandbox until the live keys exist: with PAYPAL_CLIENT_ID + PAYPAL_SECRET set the live PayPal is used, otherwise
// PAYPAL_SANDBOX_CLIENT_ID + PAYPAL_SANDBOX_SECRET (PayPal's practice mode with fake money).

export const TIERS = {
  neighborhood: { name: "Neighborhood", price: 19, radii: [10] },
  citywide: { name: "Citywide", price: 39, radii: [10, 25] },
  everywhere: { name: "Everywhere", price: 69, radii: [3, 10, 25] },
};
export const FOUNDING_SPOTS = 15;
const SHOP_KEY = /^[a-z0-9.\-]{1,80}@-?\d{1,3}\.\d{2},-?\d{1,3}\.\d{2}$/;
const SUB_ID = /^I-[A-Z0-9]{6,40}$/;
const WEBHOOK_EVENTS = [
  "BILLING.SUBSCRIPTION.ACTIVATED", "BILLING.SUBSCRIPTION.RE-ACTIVATED", "BILLING.SUBSCRIPTION.UPDATED",
  "BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED",
  "BILLING.SUBSCRIPTION.PAYMENT.FAILED", "PAYMENT.SALE.COMPLETED",
];

function out(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*", ...extra },
  });
}

export function paypalMode(env) {
  return env.PAYPAL_CLIENT_ID && env.PAYPAL_SECRET ? "live" : (env.PAYPAL_SANDBOX_CLIENT_ID && env.PAYPAL_SANDBOX_SECRET ? "sandbox" : null);
}

function creds(env) {
  const mode = paypalMode(env);
  if (!mode) return null;
  return mode === "live"
    ? { mode, id: env.PAYPAL_CLIENT_ID, secret: env.PAYPAL_SECRET, base: "https://api-m.paypal.com" }
    : { mode, id: env.PAYPAL_SANDBOX_CLIENT_ID, secret: env.PAYPAL_SANDBOX_SECRET, base: "https://api-m.sandbox.paypal.com" };
}

let tokenCache = null;
async function paypal(env, method, path, body) {
  const c = creds(env);
  if (!c) throw new Error("paypal not configured");
  if (!tokenCache || tokenCache.mode !== c.mode || tokenCache.until < Date.now()) {
    const r = await fetch(c.base + "/v1/oauth2/token", {
      method: "POST",
      headers: { authorization: "Basic " + btoa(c.id + ":" + c.secret), "content-type": "application/x-www-form-urlencoded" },
      body: "grant_type=client_credentials",
    });
    const t = await r.json();
    if (!r.ok || !t.access_token) throw new Error("paypal auth " + r.status);
    tokenCache = { mode: c.mode, token: t.access_token, until: Date.now() + (Number(t.expires_in || 300) - 60) * 1000 };
  }
  const r = await fetch(c.base + path, {
    method,
    headers: { authorization: "Bearer " + tokenCache.token, "content-type": "application/json", prefer: "return=representation" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text.slice(0, 300) }; }
  if (!r.ok) {
    const e = new Error(`paypal ${method} ${path} -> ${r.status}`);
    e.status = r.status; e.data = data;
    throw e;
  }
  return data;
}

async function config(env, mode) {
  const row = await env.DB.prepare("SELECT v FROM paypal_config WHERE k = ?").bind(mode).first();
  return row ? JSON.parse(row.v) : null;
}

// One-time (idempotent): the PayPal product, the 6 monthly plans and the webhook, for the current mode.
export async function setup(env, workerBase) {
  const c = creds(env);
  if (!c) return { ok: false, error: "No PayPal keys yet" };
  const have = await config(env, c.mode);
  if (have && have.plans && have.webhook) return { ok: true, mode: c.mode, already: true };
  const cfg = have || { plans: {} };
  if (!cfg.product) {
    const p = await paypal(env, "POST", "/v1/catalogs/products", {
      name: "Knewdle NOW Featured", description: "Featured listing for ramen shops in the Knewdle NOW app",
      type: "SERVICE", category: "SOFTWARE", home_url: "https://swatsrocks.github.io/knewdle-data/",
    });
    cfg.product = p.id;
  }
  for (const [key, t] of Object.entries(TIERS)) {
    for (const founding of [false, true]) {
      const slot = key + (founding ? "_founding" : "");
      if (cfg.plans[slot]) continue;
      const price = (founding ? t.price / 2 : t.price).toFixed(2);
      const plan = await paypal(env, "POST", "/v1/billing/plans", {
        product_id: cfg.product,
        name: `Knewdle NOW Featured: ${t.name}${founding ? " (Founding)" : ""}`,
        description: `${t.name} featured listing, billed monthly${founding ? " (founding-shop price)" : ""}`,
        status: "ACTIVE",
        billing_cycles: [{
          frequency: { interval_unit: "MONTH", interval_count: 1 },
          tenure_type: "REGULAR", sequence: 1, total_cycles: 0,
          pricing_scheme: { fixed_price: { value: price, currency_code: "USD" } },
        }],
        payment_preferences: { auto_bill_outstanding: true, payment_failure_threshold: 2 },
      });
      cfg.plans[slot] = plan.id;
    }
  }
  if (!cfg.webhook) {
    const url = workerBase + "/paypal/webhook";
    try {
      const w = await paypal(env, "POST", "/v1/notifications/webhooks", { url, event_types: WEBHOOK_EVENTS.map((name) => ({ name })) });
      cfg.webhook = w.id;
    } catch (e) {
      // Already registered (e.g. a re-run): find it.
      const list = await paypal(env, "GET", "/v1/notifications/webhooks");
      const hit = (list.webhooks || []).find((w) => w.url === url);
      if (!hit) throw e;
      cfg.webhook = hit.id;
    }
  }
  await env.DB.prepare("INSERT INTO paypal_config (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
    .bind(c.mode, JSON.stringify(cfg)).run();
  return { ok: true, mode: c.mode, plans: Object.keys(cfg.plans).length };
}

async function foundingUsed(env, mode) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM featured_subs WHERE mode = ? AND founding = 1 AND activated_at IS NOT NULL")
    .bind(mode).first();
  return r.n || 0;
}

// Public: what the Get Featured page needs (prices, PayPal client ID and plan IDs, founding spots left).
export async function offer(env) {
  const c = creds(env);
  if (!c) return out({ ok: false, error: "Featured isn't open yet" }, 503);
  const cfg = await config(env, c.mode);
  if (!cfg || !cfg.plans) return out({ ok: false, error: "Featured isn't open yet" }, 503);
  const left = Math.max(0, FOUNDING_SPOTS - await foundingUsed(env, c.mode));
  const tiers = Object.entries(TIERS).map(([key, t]) => ({
    key, name: t.name, price: t.price, founding_price: t.price / 2, radii: t.radii,
    plan: left > 0 ? cfg.plans[key + "_founding"] : cfg.plans[key],
  }));
  return out({ ok: true, mode: c.mode, client_id: c.id, founding_spots: FOUNDING_SPOTS, founding_left: left, tiers },
    200, { "cache-control": "no-store" });
}

function planInfo(cfg, planId) {
  for (const [slot, id] of Object.entries(cfg.plans || {})) {
    if (id === planId) return { tier: slot.replace("_founding", ""), founding: slot.endsWith("_founding") ? 1 : 0 };
  }
  return null;
}

const MONTH_MS = 31 * 86400000;
const GRACE_MS = 2 * 86400000;

// Reads one subscription back from PayPal and stores what it means. Returns {row, change} or null.
async function sync(env, subId, shop) {
  const c = creds(env);
  const cfg = await config(env, c.mode);
  let s;
  try { s = await paypal(env, "GET", "/v1/billing/subscriptions/" + subId); } catch (e) { if (e.status === 404) return null; throw e; }
  const info = planInfo(cfg, s.plan_id);
  if (!info) return null;   // not one of our plans
  const key = String(s.custom_id || shop?.k || "");
  if (!SHOP_KEY.test(key)) return null;
  const status = String(s.status || "");
  const now = Date.now();
  const last = Date.parse(s.billing_info?.last_payment?.time || "") || 0;
  const next = Date.parse(s.billing_info?.next_billing_time || "") || 0;
  const prev = await env.DB.prepare("SELECT * FROM featured_subs WHERE sub_id = ?").bind(subId).first();
  // Paid through: the next bill date while active; after a cancel, a month after the last payment.
  const paidThrough = Math.max(prev?.paid_through || 0, next || 0, last ? last + MONTH_MS : 0);
  const activated = prev?.activated_at || (status === "ACTIVE" ? now : null);
  const row = {
    sub_id: subId, mode: c.mode, shop: key,
    name: String(shop?.n || prev?.name || key.split("@")[0]).slice(0, 100),
    la: Number.isFinite(Number(shop?.la)) ? Number(shop.la) : (prev?.la ?? null),
    lo: Number.isFinite(Number(shop?.lo)) ? Number(shop.lo) : (prev?.lo ?? null),
    address: String(shop?.a || prev?.address || "").slice(0, 160) || null,
    tier: info.tier, founding: info.founding, status, paid_through: paidThrough,
    activated_at: activated, payer: String(s.subscriber?.email_address || prev?.payer || "").slice(0, 120) || null,
    paused: prev?.paused || 0, updated_at: now, last_payment: last || prev?.last_payment || null,
  };
  await env.DB.prepare(
    "INSERT INTO featured_subs (sub_id, mode, shop, name, la, lo, address, tier, founding, status, paid_through, activated_at, payer, paused, updated_at, last_payment) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sub_id) DO UPDATE SET shop = excluded.shop, name = excluded.name, " +
    "la = excluded.la, lo = excluded.lo, address = excluded.address, tier = excluded.tier, founding = excluded.founding, " +
    "status = excluded.status, paid_through = excluded.paid_through, activated_at = excluded.activated_at, payer = excluded.payer, " +
    "updated_at = excluded.updated_at, last_payment = excluded.last_payment"
  ).bind(row.sub_id, row.mode, row.shop, row.name, row.la, row.lo, row.address, row.tier, row.founding, row.status,
    row.paid_through, row.activated_at, row.payer, row.paused, row.updated_at, row.last_payment).run();
  let change = null;
  if (status === "ACTIVE" && !prev?.activated_at) change = "new";
  else if (prev && prev.status !== status) change = status.toLowerCase();
  else if (prev && row.last_payment && row.last_payment !== prev.last_payment) change = "paid";
  else if (prev && prev.tier !== row.tier) change = "tier";
  return { row, change };
}

function isLive(r, now = Date.now()) {
  if (r.paused) return false;
  if (r.status === "ACTIVE") return true;
  return r.status === "CANCELLED" && now < (r.paid_through || 0) + GRACE_MS;
}

function alertText(row, change) {
  const t = TIERS[row.tier]?.name || row.tier;
  const price = (TIERS[row.tier]?.price || 0) / (row.founding ? 2 : 1);
  switch (change) {
    case "new": return { title: `🎉 New Featured shop: ${row.name}`, text: `${t}${row.founding ? " (founding)" : ""} · $${price}/mo · live in the app now` };
    case "paid": return { title: `💰 Payment: ${row.name}`, text: `$${price} · ${t}` };
    case "tier": return { title: `⬆ Plan change: ${row.name}`, text: `Now ${t}` };
    case "cancelled": return { title: `👋 Cancelled: ${row.name}`, text: `Featured until ${new Date(row.paid_through).toDateString()}` };
    case "suspended": return { title: `⚠️ Payment failed: ${row.name}`, text: "PayPal suspended it; it's off the Featured list until they pay" };
    case "expired": return { title: `Featured ended: ${row.name}`, text: t };
    case "active": return { title: `✅ Back on: ${row.name}`, text: t };
    default: return null;
  }
}

async function tell(env, push, row, change) {
  const a = alertText(row, change);
  if (a && push) await push({ kind: "sub", change, shop: row.name, tier: row.tier, title: a.title, text: a.text, ts: Date.now() });
}

// The page calls this right after PayPal approves the subscription.
export async function start(request, env, push) {
  const c = creds(env);
  if (!c) return out({ ok: false, error: "Featured isn't open yet" }, 503);
  let body;
  try { body = await request.json(); } catch { return out({ ok: false, error: "bad json" }, 400); }
  const subId = String(body.subscription_id || "");
  const shop = body.shop || {};
  if (!SUB_ID.test(subId) || !SHOP_KEY.test(String(shop.k || ""))) return out({ ok: false, error: "bad request" }, 400);
  const r = await sync(env, subId, shop);
  if (!r) return out({ ok: false, error: "We couldn't find that subscription at PayPal" }, 404);
  if (r.change) await tell(env, push, r.row, r.change);
  return out({ ok: true, featured: isLive(r.row), status: r.row.status, tier: r.row.tier, founding: !!r.row.founding });
}

// PayPal webhook: only a hint about which subscription changed; the truth is read back from PayPal.
export async function webhook(request, env, push) {
  if (!creds(env)) return out({ ok: true });
  let ev;
  try { ev = await request.json(); } catch { return out({ ok: false }, 400); }
  const res = ev?.resource || {};
  const subId = String(ev?.event_type || "").startsWith("PAYMENT.SALE") ? String(res.billing_agreement_id || "") : String(res.id || "");
  if (!SUB_ID.test(subId)) return out({ ok: true, ignored: true });
  const r = await sync(env, subId, null);
  if (r && r.change) await tell(env, push, r.row, r.change);
  if (r && ev.event_type === "BILLING.SUBSCRIPTION.PAYMENT.FAILED" && r.row.status === "ACTIVE" && push) {
    await push({ kind: "sub", change: "failed", shop: r.row.name, tier: r.row.tier, title: `⚠️ Payment failed: ${r.row.name}`,
      text: "PayPal will retry automatically; still featured for now", ts: Date.now() });
  }
  return out({ ok: true });
}

// Hourly: re-read every subscription that could still change, so a missed webhook never matters.
export async function resync(env, push) {
  if (!creds(env)) return;
  const mode = paypalMode(env);
  const { results } = await env.DB.prepare(
    "SELECT sub_id FROM featured_subs WHERE mode = ? AND (status IN ('APPROVAL_PENDING', 'APPROVED', 'ACTIVE', 'SUSPENDED') " +
    "OR (status = 'CANCELLED' AND paid_through > ?)) ORDER BY updated_at LIMIT 40"
  ).bind(mode, Date.now() - 3 * 86400000).all();
  for (const { sub_id } of results) {
    try {
      const r = await sync(env, sub_id, null);
      if (r && r.change) await tell(env, push, r.row, r.change);
    } catch (e) { /* next hour */ }
  }
}

// For /overrides: the shops featured right now.
// Everyone sees only real (live) subscriptions. Sandbox test ones show only to a phone in developer test mode (?test=1).
export async function featuredList(env, test = false) {
  const { results } = await env.DB.prepare(
    "SELECT shop, name, la, lo, tier, status, paid_through, paused FROM featured_subs WHERE mode = 'live'" + (test ? " OR mode = 'sandbox'" : ""))
    .all();
  const now = Date.now();
  const best = new Map();
  for (const r of results) {
    if (!isLive(r, now)) continue;
    const rank = TIERS[r.tier] ? Object.keys(TIERS).indexOf(r.tier) : -1;
    const had = best.get(r.shop);
    if (!had || rank > had.rank) best.set(r.shop, { rank, e: { k: r.shop, n: r.name, ...(r.la != null ? { la: r.la, lo: r.lo } : {}), t: r.tier, r: TIERS[r.tier].radii } });
  }
  return [...best.values()].map((x) => x.e);
}

// Pulse: every subscription, the totals and monthly revenue.
export async function pulseSubs(env) {
  const mode = paypalMode(env);
  if (!mode) return { mode: null, subs: [], active: 0, mrr: 0, founding_left: FOUNDING_SPOTS };
  const { results } = await env.DB.prepare("SELECT * FROM featured_subs WHERE mode = ? ORDER BY updated_at DESC LIMIT 200").bind(mode).all();
  const now = Date.now();
  let mrr = 0, active = 0;
  const subs = results.map((r) => {
    const live = isLive(r, now);
    const price = (TIERS[r.tier]?.price || 0) / (r.founding ? 2 : 1);
    if (r.status === "ACTIVE") { mrr += price; active += 1; }
    return { id: r.sub_id, shop: r.name, key: r.shop, address: r.address, tier: r.tier, founding: !!r.founding, status: r.status,
      featured: live, paused: !!r.paused, price, paid_through: r.paid_through, payer: r.payer, last_payment: r.last_payment };
  });
  return { mode, subs, active, mrr: Math.round(mrr * 100) / 100, founding_left: Math.max(0, FOUNDING_SPOTS - await foundingUsed(env, mode)) };
}

// Pulse: pause (hide) or unpause a subscription, or cancel it at PayPal (stops billing).
export async function pulseSubAction(request, env) {
  let body;
  try { body = await request.json(); } catch { return out({ ok: false, error: "bad json" }, 400); }
  const id = String(body.id || "");
  if (!SUB_ID.test(id)) return out({ ok: false, error: "bad request" }, 400);
  if (body.action === "pause" || body.action === "unpause") {
    await env.DB.prepare("UPDATE featured_subs SET paused = ?, updated_at = ? WHERE sub_id = ?")
      .bind(body.action === "pause" ? 1 : 0, Date.now(), id).run();
    return out({ ok: true });
  }
  if (body.action === "cancel") {
    await paypal(env, "POST", `/v1/billing/subscriptions/${id}/cancel`, { reason: "Cancelled by Knewdle NOW" });
    await sync(env, id, null);
    return out({ ok: true });
  }
  return out({ ok: false, error: "bad action" }, 400);
}
