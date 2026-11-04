// Live "Open now" for each spot, from its posted weekly hours (Monday first, minutes after midnight),
// worked out in the shop's own time zone. Plus the filter chips.
(function () {
  "use strict";
  var DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  function clock(m) {
    m = ((m % 1440) + 1440) % 1440;
    if (m === 0) return "midnight";
    if (m === 720) return "noon";
    var h = Math.floor(m / 60), mi = m % 60;
    return (h % 12 || 12) + (mi ? ":" + String(mi).padStart(2, "0") : "") + (h < 12 ? " AM" : " PM");
  }
  function nowIn(tz) {
    try {
      var p = {};
      new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "numeric", minute: "numeric", hourCycle: "h23" })
        .formatToParts(new Date()).forEach(function (x) { p[x.type] = x.value; });
      var d = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(p.weekday);
      return { d: d, m: (parseInt(p.hour, 10) % 24) * 60 + parseInt(p.minute, 10) };
    } catch (e) {
      var n = new Date();
      return { d: (n.getDay() + 6) % 7, m: n.getHours() * 60 + n.getMinutes() };
    }
  }
  function status(h, tz) {
    var t = nowIn(tz), d = t.d, m = t.m, i, s;
    var y = h[(d + 6) % 7] || [];
    for (i = 0; i < y.length; i++) if (y[i][1] > 1440 && m < y[i][1] - 1440) return openAt(y[i][1] - 1440 - m, y[i][1]);
    var today = h[d] || [];
    for (i = 0; i < today.length; i++) if (m >= today[i][0] && m < today[i][1]) return openAt(today[i][1] - m, today[i][1]);
    var later = today.filter(function (x) { return x[0] > m; }).sort(function (a, b) { return a[0] - b[0]; });
    if (later.length) return { open: false, cls: "closed", text: "Closed · opens " + clock(later[0][0]) };
    for (var k = 1; k <= 7; k++) {
      var day = h[(d + k) % 7] || [];
      if (!day.length) continue;
      s = day.slice().sort(function (a, b) { return a[0] - b[0]; })[0];
      return { open: false, cls: "closed", text: "Closed · opens " + clock(s[0]) + " " + (k === 1 ? "tomorrow" : DAYS[(d + k) % 7]) };
    }
    return { open: false, cls: "closed", text: "Closed" };
  }
  function openAt(left, close) {
    return left <= 45 ? { open: true, cls: "soon", text: "Closes soon · " + clock(close) }
                      : { open: true, cls: "open", text: "Open now · until " + clock(close) };
  }

  var cards = Array.prototype.slice.call(document.querySelectorAll(".spot"));
  function paint() {
    var openCount = 0, listOpen = 0;
    cards.forEach(function (c) {
      var raw = c.getAttribute("data-h");
      if (!raw) return;
      var st;
      try { st = status(JSON.parse(raw), c.getAttribute("data-tz")); } catch (e) { return; }
      var el = c.querySelector(".status");
      el.innerHTML = "";
      var b = document.createElement("span");
      b.className = "s " + st.cls;
      b.textContent = st.open ? (st.cls === "soon" ? "Closes soon" : "Open now") : "Closed";
      el.appendChild(b);
      var w = c.querySelector(".when");
      if (!w) { w = document.createElement("p"); w.className = "when"; c.querySelector(".addr").after(w); }
      w.textContent = st.text;
      c.dataset.open = st.open ? "1" : "0";
      if (st.open) { openCount++; if (c.closest("[data-list]")) listOpen++; }
    });
    var live = document.querySelector("[data-live]");
    if (live) {
      live.textContent = listOpen ? listOpen + " open right now" : "none open right now";
      live.classList.toggle("on", listOpen > 0);
    }
  }
  // Open spots first (then closing soon, then hours unknown, then closed), keeping the page's order otherwise.
  var listEl = document.querySelector("[data-list]");
  function reorder() {
    if (!listEl) return;
    var rank = function (c) { return c.dataset.open === "1" ? 0 : c.getAttribute("data-h") ? 2 : 1; };
    Array.prototype.slice.call(listEl.children).map(function (c, i) { return [rank(c), i, c]; })
      .sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; })
      .forEach(function (x) { listEl.appendChild(x[2]); });
  }
  paint();
  reorder();
  setInterval(paint, 60000);

  // City search on the index page.
  var q = document.querySelector("[data-find]"), all = document.querySelector("[data-all]"), top = document.querySelector("[data-top]");
  if (q && all) {
    var links = Array.prototype.slice.call(all.querySelectorAll("a"));
    q.addEventListener("input", function () {
      var t = q.value.trim().toLowerCase(), n = 0;
      all.hidden = !t; if (top) top.hidden = !!t;
      links.forEach(function (a) { var ok = t && a.textContent.toLowerCase().indexOf(t) !== -1 && n < 40; a.hidden = !ok; if (ok) n++; });
    });
  }

  var chips = document.querySelectorAll(".chip");
  var list = document.querySelector("[data-list]");
  var empty = document.querySelector("[data-empty]");
  Array.prototype.forEach.call(chips, function (ch) {
    ch.addEventListener("click", function () {
      Array.prototype.forEach.call(chips, function (x) { x.classList.toggle("on", x === ch); });
      var f = ch.getAttribute("data-f"), shown = 0;
      Array.prototype.forEach.call(list.querySelectorAll(".spot"), function (c) {
        var ok = f === "all" ||
          (f === "open" && c.dataset.open === "1") ||
          (f === "late" && !!c.querySelector(".k-late")) ||
          (f === "shop" && c.getAttribute("data-kind") !== "menu") ||
          (f === "menu" && c.getAttribute("data-kind") === "menu");
        c.hidden = !ok;
        if (ok) shown++;
      });
      if (empty) empty.hidden = shown > 0;
    });
  });
})();
