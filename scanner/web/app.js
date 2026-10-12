// F&O Scanner dashboard — plain JS, no build step, talks only to this server's /api.
(() => {
  "use strict";

  const REFRESH_MS = 5000;
  const TABS = {
    previous_close: { api: "previous-close", label: "Previous close", price: "previous_close", priceLabel: "Close",
                      base: "prior_close", baseLabel: "Prior close" },
    premarket: { api: "premarket", label: "Pre-open", price: "preopen_price", priceLabel: "Pre-open",
                 base: "previous_close", baseLabel: "Prev close" },
    live: { api: "live", label: "Live", price: "ltp", priceLabel: "LTP", base: "previous_close", baseLabel: "Prev close" },
  };

  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* storage unavailable */ } },
  };

  // Scanner view: the three lists use each dataset's own threshold, inclusive
  // (>= +T or <= -T), plus their intersection. Read-only; nothing is recomputed server-side.
  const SCAN = {
    previous_close: { label: "Previous Market", price: "previous_close", priceLabel: "Close" },
    premarket: { label: "Pre-Market", price: "preopen_price", priceLabel: "Pre-open" },
    live: { label: "Current Live Market", price: "ltp", priceLabel: "LTP" },
  };

  const state = {
    view: store.get("fno_view") === "details" ? "details" : "scanner",
    tab: store.get("fno_tab") || "live",
    day: "",                     // "" = today (latest), else a snapshot date
    token: store.get("fno_token") || "",
    data: {},                    // dataset -> doc
    stale: {},                   // dataset -> true while showing a kept copy after a 404
    etags: {},                   // url -> etag
    health: null,
    status: null,
    sector: "",
    search: "",
    hlOnly: false,
    sort: { key: "change_percent", dir: null },   // null dir = natural (gainers desc, losers asc)
    timer: null,
  };
  if (!TABS[state.tab]) state.tab = "live";

  // ---------------------------------------------------------------- fetch --
  class AuthError extends Error {}

  async function getJSON(url, { conditional = false } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (conditional && state.etags[url]) headers["If-None-Match"] = state.etags[url];
    const res = await fetch(url, { headers, cache: "no-store" });
    if (res.status === 401) throw new AuthError();
    if (res.status === 304) return { unchanged: true };
    if (res.status === 404) return { missing: true, body: await res.json().catch(() => null) };
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    const etag = res.headers.get("ETag");
    if (etag) state.etags[url] = etag;
    return { body: await res.json() };
  }

  function datasetUrl(ds) {
    const api = ds === "sectors" ? "sectors" : TABS[ds] ? TABS[ds].api : ds;
    return state.day ? `/api/snapshots/${state.day}/${api}` : `/api/${api}`;
  }

  async function loadDataset(ds) {
    const r = await getJSON(datasetUrl(ds), { conditional: true });
    if (r.unchanged) return false;
    // Previous Market must never disappear on refresh: on a 404 keep the last
    // good copy and mark it stale instead of replacing it with nothing.
    if (r.missing && ds === "previous_close" && state.data[ds]) {
      if (state.stale[ds]) return false;
      state.stale[ds] = true;
      return true;
    }
    state.data[ds] = r.missing ? null : r.body;
    state.stale[ds] = false;
    return true;
  }

  async function refresh() {
    const ind = $("refresh");
    try {
      if (!state.day) {
        const h = await getJSON("/api/health");
        state.health = h.body;
        state.status = h.body ? { jobs: h.body.jobs, alerts: h.body.alerts } : null;
      } else {
        const s = await getJSON(datasetUrl("status"), { conditional: true });
        if (!s.unchanged) state.status = s.missing ? null : s.body;
      }
      const wanted = state.view === "scanner"
        ? ["previous_close", "premarket", "live", "sectors"]
        : [state.tab, "sectors"];
      const changed = (await Promise.all(wanted.map(loadDataset))).some(Boolean);
      renderHeader();
      renderStatus();
      renderTokenBanner();
      if (changed || !renderedOnce) render();
      ind.className = "refresh on";
      setTimeout(() => ind.classList.remove("on"), 600);
    } catch (e) {
      if (e instanceof AuthError) return askToken();
      ind.className = "refresh err";
      ind.title = `Refresh failed: ${e.message}`;
    }
  }

  function schedule() {
    clearInterval(state.timer);
    state.timer = null;
    if (!state.day) {
      state.timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
    }
  }

  // ----------------------------------------------------------------- token --
  function askToken(rejected = state.token !== "") {
    if (rejected) { state.token = ""; store.del("fno_token"); }
    $("tokenError").hidden = !rejected;
    const dlg = $("tokenDialog");
    if (!dlg.open) dlg.showModal();
  }

  $("tokenForm").addEventListener("submit", () => {
    state.token = $("tokenInput").value.trim();
    $("tokenInput").value = "";
    store.set("fno_token", state.token);
    refresh().then(loadDays);
  });

  // ------------------------------------------------------------- helpers --
  const fmtPct = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
  const fmtNum = (v) => (v === null || v === undefined ? "—"
    : v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

  function el(tag, attrs = {}, ...children) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) n.append(c);
    return n;
  }

  function age(seconds) {
    if (seconds === null || seconds === undefined) return "";
    if (seconds < 90) return `${Math.round(seconds)}s ago`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
  }

  function timeOf(iso) { return iso ? iso.slice(11, 19) : "—"; }

  // -------------------------------------------------------------- render --
  let renderedOnce = false;

  function renderHeader() {
    const h = state.health;
    const doc = state.data[state.tab];
    $("tradeDate").textContent = state.day ? `${state.day} (history)` : (h ? h.trade_date : (doc && doc.trade_date) || "—");
    const phase = $("phase");
    const p = state.day ? "HISTORY" : (h ? h.market_phase : "—");
    phase.textContent = p.replace("_", "-");
    phase.className = `pill ${p}`;
  }

  function renderStatus() {
    const bar = $("statusBar");
    bar.replaceChildren();
    const names = { universe: "Universe", previous_close: "Prev close", premarket: "Pre-open", live: "Live", sectors: "Sectors" };
    if (!state.day && state.health) {
      for (const [ds, f] of Object.entries(state.health.files)) {
        const s = f.available ? f.status : "missing";
        const stale = f.available && f.trade_date !== state.health.trade_date;
        bar.append(el("span", { class: "chip", title: f.generated_at ? `Generated ${f.generated_at}` : "No file yet" },
          el("span", { class: `dot ${stale ? "stale" : s}` }),
          el("b", { text: names[ds] }),
          el("span", { class: "muted", text: f.available ? `${stale ? "old · " : ""}${s} · ${age(f.age_seconds)}` : "not yet" })));
      }
      const tok = state.health.token;
      if (tok) {
        const left = tok.hours_left;
        const txt = tok.state === "expired" ? "expired"
          : tok.state === "renewal_failed" ? "renewal failed"
          : left === null ? tok.state : `${left >= 1 ? `${Math.floor(left)}h` : `${Math.round(left * 60)}m`} left`;
        bar.append(el("span", { class: "chip", title: tok.message || "" },
          el("span", { class: `dot ${tok.state === "ok" ? "ok" : tok.state === "expiring" ? "stale" : "failed"}` }),
          el("b", { text: "Dhan token" }), el("span", { class: "muted", text: `${tok.method || ""} · ${txt}` })));
      }
      const live = state.data.live;
      if (live && live.feed && (state.view === "scanner" || state.tab === "live")) {
        bar.append(el("span", { class: "chip" },
          el("span", { class: `dot ${live.feed.state === "connected" ? "ok" : "stale"}` }),
          el("b", { text: "Feed" }),
          el("span", { class: "muted", text: `${live.feed.state} · last tick ${timeOf(live.feed.last_tick_at)} · ${live.feed.reconnects} reconnects` })));
      }
    } else if (state.day) {
      for (const ds of Object.keys(names)) {
        const doc = state.data[ds];
        if (!doc) continue;
        bar.append(el("span", { class: "chip" }, el("span", { class: `dot ${doc.status}` }),
          el("b", { text: names[ds] }), el("span", { class: "muted", text: doc.status })));
      }
    }
    const jobs = (state.status && state.status.jobs) || {};
    const failed = Object.entries(jobs).filter(([, j]) => ["failed", "missing"].includes(j.result));
    for (const [name, j] of failed) {
      bar.append(el("span", { class: "chip", title: j.message },
        el("span", { class: "dot failed" }), el("b", { text: name }), el("span", { class: "muted", text: j.result })));
    }
    const alerts = (state.status && state.status.alerts) || [];
    const box = $("alerts");
    if (alerts.length) {
      bar.append(el("span", { class: "chip btn", onclick: () => { box.hidden = !box.hidden; } },
        el("span", { class: "dot partial" }), el("b", { text: `${alerts.length} alert${alerts.length > 1 ? "s" : ""}` })));
      box.replaceChildren(el("b", { text: "Alerts" }),
        el("ul", {}, ...alerts.map((a) => el("li", { text: `[${a.job}] ${a.message}` }))));
    } else {
      box.hidden = true;
    }
  }

  function renderTokenBanner() {
    const b = $("tokenBanner");
    const tok = !state.day && state.health && state.health.token;
    b.hidden = true;
    if (!tok || tok.state === "ok" || tok.state === "unknown") return;
    const msgs = {
      expired: "Dhan token has expired — generate a new one on web.dhan.co and update DHAN_ACCESS_TOKEN in .env.",
      renewal_failed: `Dhan token renewal failed: ${tok.message || "see logs"}.`,
      expiring: `Dhan token expires at ${timeOf(tok.expires_at)} (${Math.round((tok.hours_left || 0) * 60)} min left).`,
    };
    b.textContent = msgs[tok.state] || `Dhan token: ${tok.state}`;
    b.className = `banner ${tok.state === "expiring" ? "warn" : ""}`.trim();
    b.hidden = false;
  }

  function renderBanner(doc, label = TABS[state.tab].label) {
    const b = $("banner");
    b.hidden = true;
    if (!doc) return;
    const today = state.health && state.health.trade_date;
    let msg = null, kind = "";
    if (!state.day && today && doc.trade_date !== today) {
      msg = `Showing data from ${doc.trade_date}; today's ${label.toLowerCase()} data is not built yet.`;
      kind = "info";
    } else if (doc.status === "missing") {
      msg = `No ${label.toLowerCase()} data: ${doc.status_reason || "data unavailable"}`;
    } else if (doc.status === "stale") {
      msg = `Stale: ${doc.status_reason || "feed not updating"} — prices may be out of date.`;
      kind = "warn";
    } else if (doc.status === "partial") {
      msg = `Partial: ${doc.status_reason || "some stocks have no data"}`;
      kind = "warn";
    }
    if (msg) { b.textContent = msg; b.className = `banner ${kind}`; b.hidden = false; }
  }

  function renderSectors() {
    const list = $("sectorList");
    const empty = $("sectorEmpty");
    const doc = state.data.sectors;
    const block = doc && doc[state.tab];
    list.replaceChildren();
    if (!block) {
      empty.hidden = false;
      empty.textContent = "No sector data for this view yet.";
      $("sectorMeta").textContent = "";
      return;
    }
    empty.hidden = true;
    const s = block.summary;
    $("sectorMeta").textContent = `±${block.thresholds.up}% · ${s.advancing}▲ ${s.declining}▼`;
    const maxAbs = Math.max(1, ...block.sectors.map((r) => Math.abs(r.avg_change_percent || 0)));
    for (const r of block.sectors) {
      const v = r.avg_change_percent;
      const w = v === null ? 0 : (Math.abs(v) / maxAbs) * 50;
      const bar = el("i", { style: `width:${w}%;${v >= 0 ? "left:50%" : `left:${50 - w}%`};background:var(${v >= 0 ? "--bar-up" : "--bar-down"})` });
      const li = el("li", {
        class: `${state.sector === r.sector ? "active" : ""} ${r.highlight ? `hl-${r.highlight}` : ""}`.trim(),
        title: `${r.sector}: ${r.priced}/${r.constituents} priced, +${r.advances} / -${r.declines}` +
               (r.top_gainer ? `\nTop: ${r.top_gainer.symbol} ${fmtPct(r.top_gainer.change_percent)}` : "") +
               (r.top_loser ? `\nBottom: ${r.top_loser.symbol} ${fmtPct(r.top_loser.change_percent)}` : ""),
        onclick: () => { state.sector = state.sector === r.sector ? "" : r.sector; $("sectorFilter").value = state.sector; render(); },
      },
        el("span", { class: "rank", text: r.rank ?? "–" }),
        el("span", { class: "name" },
          el("span", { text: r.sector }),
          el("span", { class: "meta", text: `${r.priced}/${r.constituents} · +${r.advances} −${r.declines}${r.status !== "ok" ? ` · ${r.status}` : ""}` }),
          el("span", { class: "bar" }, bar)),
        el("span", { class: `val ${cls(v)}` }, fmtPct(v), r.highlight ? el("span", { class: `tag ${r.highlight}`, text: r.highlight === "UP" ? "▲" : "▼" }) : null));
      list.append(li);
    }
  }

  function filterRecords(recs) {
    const q = state.search.trim().toUpperCase();
    return recs.filter((r) =>
      (!q || r.symbol.includes(q)) &&
      (!state.sector || (sectorOf(r.symbol) || r.sector) === state.sector) &&
      (!state.hlOnly || r.highlight));
  }

  // Sector for a symbol from the current sectors.json (records may carry an older label).
  let sectorIndex = { block: null, map: new Map() };
  function sectorOf(sym) {
    const block = state.data.sectors && state.data.sectors[state.tab];
    if (!block) return null;
    if (sectorIndex.block !== block) {
      const map = new Map();
      for (const s of block.sectors) for (const x of s.symbols || []) map.set(x, s.sector);
      sectorIndex = { block, map };
    }
    return sectorIndex.map.get(sym) || null;
  }

  function sortRecords(recs, natural) {
    const { key, dir } = state.sort;
    if (!dir) return natural === "asc" ? [...recs].sort((a, b) => a.change_percent - b.change_percent) : recs;
    const m = dir === "asc" ? 1 : -1;
    return [...recs].sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === y) return a.symbol.localeCompare(b.symbol);
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * m;
    });
  }

  function columns() {
    const t = TABS[state.tab];
    return [
      { key: "symbol", label: "Symbol" },
      { key: "sector", label: "Sector", cls: "sec" },
      { key: t.price, label: t.priceLabel, num: true },
      { key: t.base, label: t.baseLabel, num: true, cls: "base" },
      { key: "change", label: "Chg", num: true },
      { key: "change_percent", label: "%", num: true },
    ];
  }

  function renderTable(id, recs, natural) {
    const table = $(id);
    const cols = columns();
    const head = el("tr", {}, ...cols.map((c) => el("th", {
      class: `${c.cls || ""} ${state.sort.key === c.key && state.sort.dir ? "sorted" : ""}`.trim(),
      onclick: () => {
        const s = state.sort;
        s.dir = s.key !== c.key ? "desc" : s.dir === "desc" ? "asc" : s.dir === "asc" ? null : "desc";
        s.key = c.key;
        render();
      },
      text: c.label + (state.sort.key === c.key && state.sort.dir ? (state.sort.dir === "asc" ? " ↑" : " ↓") : ""),
    })));
    const rows = sortRecords(recs, natural).map((r) => {
      const flags = [];
      if (r.highlight) flags.push(el("span", { class: `tag ${r.highlight}`, text: r.highlight === "UP" ? "▲" : "▼" }));
      if (r.record_status && r.record_status !== "ok") flags.push(el("span", { class: "tag warn", text: r.record_status.replace("_", " ") }));
      if (r.prev_close_check === "mismatch") flags.push(el("span", { class: "tag warn", title: "Previous close sources disagree", text: "chk" }));
      return el("tr", { class: r.highlight ? `hl-${r.highlight}` : null },
        el("td", { class: "sym" }, r.symbol, ...flags),
        el("td", { class: "sec", title: sectorOf(r.symbol) || r.sector, text: sectorOf(r.symbol) || r.sector }),
        ...cols.slice(2).map((c) => el("td", {
          class: [c.cls, c.key === "change" || c.key === "change_percent" ? cls(r.change_percent) : ""]
            .filter(Boolean).join(" ") || null,
          text: c.key === "change_percent" ? fmtPct(r[c.key]) : fmtNum(r[c.key]),
        })));
    });
    table.replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
    if (!rows.length) table.append(el("tbody", {}, el("tr", {}, el("td", { colspan: cols.length, class: "empty", text: "None" }))));
  }

  function renderStocks() {
    const doc = state.data[state.tab];
    const emptyBox = $("datasetEmpty");
    if (!doc) {
      emptyBox.hidden = false;
      emptyBox.textContent = state.day
        ? `No ${TABS[state.tab].label.toLowerCase()} snapshot for ${state.day}.`
        : `No ${TABS[state.tab].label.toLowerCase()} data yet.`;
      $("summary").replaceChildren();
      for (const id of ["gainers", "losers", "unchanged"]) $(id).replaceChildren();
      for (const id of ["gCount", "lCount", "uCount"]) $(id).textContent = "";
      return;
    }
    emptyBox.hidden = true;
    const s = doc.summary;
    const parts = [
      ["Total", s.total], ["Gainers", s.gainers], ["Losers", s.losers],
      [`>+${doc.thresholds.up}%`, s.highlighted_up], [`<${doc.thresholds.down}%`, s.highlighted_down],
      ["Missing", s.missing],
    ];
    if (s.stale !== undefined) parts.push(["Stale", s.stale]);
    if (doc.close_date) parts.push(["Close date", doc.close_date]);
    const when = doc.as_of || doc.nse_timestamp || doc.generated_at;   // pre-open: NSE's snapshot time
    $("summary").replaceChildren(...parts.map(([k, v]) => el("span", {}, `${k} `, el("b", { text: v }))),
      el("span", {}, "As of ", el("b", { text: timeOf(when) })));
    const g = filterRecords(doc.gainers || []), l = filterRecords(doc.losers || []), u = filterRecords(doc.unchanged || []);
    $("gCount").textContent = `(${g.length})`;
    $("lCount").textContent = `(${l.length})`;
    $("uCount").textContent = `(${u.length})`;
    renderTable("gainers", g, "desc");
    renderTable("losers", l, "asc");
    renderTable("unchanged", u, "desc");
  }

  function renderSectorFilter() {
    const sel = $("sectorFilter");
    const block = state.data.sectors && state.data.sectors[state.tab];
    const names = block ? block.sectors.map((s) => s.sector).sort() : [];
    const current = [...sel.options].slice(1).map((o) => o.value).join("|");
    if (current !== names.join("|")) {
      sel.replaceChildren(el("option", { value: "", text: "All sectors" }), ...names.map((n) => el("option", { value: n, text: n })));
    }
    sel.value = state.sector;
  }

  // ------------------------------------------------------- scanner view --
  // Sector for any symbol, from whichever sectors.json block is available.
  function anySectorOf(sym, fallback) {
    const doc = state.data.sectors;
    if (doc) {
      for (const ds of ["live", "premarket", "previous_close"]) {
        const block = doc[ds];
        if (!block) continue;
        for (const s of block.sectors) if (s.symbols && s.symbols.includes(sym)) return s.sector;
      }
    }
    return fallback || "—";
  }

  // Stocks at or beyond the dataset's threshold, biggest move first.
  function qualifying(doc) {
    if (!doc || doc.status === "missing") return [];
    const t = doc.thresholds.up;
    const all = [...(doc.gainers || []), ...(doc.losers || []), ...(doc.unchanged || [])];
    return all
      .filter((r) => r.change_percent !== null && r.change_percent !== undefined &&
                     (r.change_percent >= t || r.change_percent <= -t))
      .sort((a, b) => Math.abs(b.change_percent) - Math.abs(a.change_percent) || a.symbol.localeCompare(b.symbol));
  }

  const dirOf = (p) => (p > 0 ? "UP" : "DOWN");
  const dirTag = (d) => el("span", { class: `dir ${d}`, text: d === "UP" ? "▲ UP" : d === "DOWN" ? "▼ DOWN" : "◆ MIXED" });

  function sectionParts(ds) {
    const sec = document.querySelector(`.scan[data-ds="${ds}"]`);
    return { meta: sec.querySelector(".scan-meta"), table: sec.querySelector(".scan-table") };
  }

  function emptyRow(table, cols, text) {
    table.replaceChildren(el("tbody", {}, el("tr", {}, el("td", { colspan: cols, class: "empty", text }))));
  }

  function renderScanList(ds) {
    const doc = state.data[ds];
    const { meta, table } = sectionParts(ds);
    const cfg = SCAN[ds];
    if (!doc) {
      meta.textContent = "";
      emptyRow(table, 5, state.day ? `No ${cfg.label.toLowerCase()} snapshot for ${state.day}.`
                                   : `Waiting for ${cfg.label.toLowerCase()} data.`);
      return [];
    }
    const t = doc.thresholds.up;
    const rows = qualifying(doc);
    const when = doc.close_date ? `close of ${doc.close_date}`
      : `as of ${timeOf(doc.as_of || doc.nse_timestamp || doc.generated_at)}`;
    const today = state.health && state.health.trade_date;
    const old = !state.day && today && doc.trade_date !== today ? ` · from ${doc.trade_date}` : "";
    meta.replaceChildren(
      `≥ +${t}% or ≤ −${t}% · `, el("b", { text: String(rows.length) }), ` stocks · ${when}${old}`,
      doc.status !== "ok" ? el("span", { class: "tag warn", text: doc.status }) : "",
      state.stale[ds] ? el("span", { class: "tag warn", title: "File not available now; showing the last good copy", text: "stale" }) : "");
    const head = el("tr", {}, el("th", { text: "Symbol" }), el("th", { class: "sec", text: "Sector" }),
      el("th", { text: cfg.priceLabel }), el("th", { text: "%" }), el("th", { text: "Direction" }));
    if (!rows.length) {
      table.replaceChildren(el("thead", {}, head));
      table.append(el("tbody", {}, el("tr", {}, el("td", {
        colspan: 5, class: "empty",
        text: doc.status === "missing" ? `No data: ${doc.status_reason || "unavailable"}` : `No stock moved ±${t}% or more.`,
      }))));
      return rows;
    }
    const body = rows.map((r) => {
      const d = dirOf(r.change_percent);
      const sector = anySectorOf(r.symbol, r.sector);
      return el("tr", { class: `hl-${d}` },
        el("td", { class: "sym" }, r.symbol, el("span", { class: "sym-sec", text: sector })),   // sector under symbol on phones
        el("td", { class: "sec", text: sector }),
        el("td", { text: fmtNum(r[cfg.price]) }),
        el("td", { class: cls(r.change_percent), text: fmtPct(r.change_percent) }),
        el("td", {}, dirTag(d)));
    });
    table.replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
    return rows;
  }

  function renderCommon(lists) {
    const { meta, table } = sectionParts("common");
    const head = el("tr", {}, el("th", { text: "Symbol" }), el("th", { text: "Previous %" }),
      el("th", { text: "Pre-Market %" }), el("th", { text: "Live %" }), el("th", { text: "Direction" }));
    const docs = Object.keys(SCAN).map((ds) => state.data[ds]);
    const missing = Object.keys(SCAN).filter((ds, i) => !docs[i] || docs[i].status === "missing")
      .map((ds) => SCAN[ds].label);
    meta.textContent = "";
    if (missing.length) {
      table.replaceChildren(el("thead", {}, head));
      table.append(el("tbody", {}, el("tr", {}, el("td", { colspan: 5, class: "empty",
        text: `Waiting for ${missing.join(", ")} data.` }))));
      return;
    }
    const dates = [...new Set(docs.map((d) => d.trade_date))];
    if (dates.length > 1) {
      table.replaceChildren(el("thead", {}, head));
      table.append(el("tbody", {}, el("tr", {}, el("td", { colspan: 5, class: "empty",
        text: `The lists are from different days (${docs.map((d, i) => `${Object.values(SCAN)[i].label} ${d.trade_date}`).join(", ")}). ` +
              "Common stocks need all three from the same day." }))));
      return;
    }
    const [prev, pre, live] = lists.map((rows) => new Map(rows.map((r) => [r.symbol, r.change_percent])));
    const rank = { UP: 0, DOWN: 1, MIXED: 2 };
    const rows = [...prev.keys()].filter((s) => pre.has(s) && live.has(s)).map((s) => {
      const p = [prev.get(s), pre.get(s), live.get(s)];
      const d = p.every((x) => x > 0) ? "UP" : p.every((x) => x < 0) ? "DOWN" : "MIXED";
      return { symbol: s, p, d };
    }).sort((a, b) => rank[a.d] - rank[b.d] || Math.abs(b.p[2]) - Math.abs(a.p[2]) || a.symbol.localeCompare(b.symbol));
    const counts = ["UP", "DOWN", "MIXED"].map((d) => `${rows.filter((r) => r.d === d).length} ${d.toLowerCase()}`);
    meta.replaceChildren(el("b", { text: String(rows.length) }), ` stocks · ${counts.join(" · ")} · ${dates[0]}`);
    if (!rows.length) {
      table.replaceChildren(el("thead", {}, head));
      table.append(el("tbody", {}, el("tr", {}, el("td", { colspan: 5, class: "empty",
        text: "No stock is in all three lists." }))));
      return;
    }
    table.replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows.map((r) =>
      el("tr", { class: r.d === "MIXED" ? null : `hl-${r.d}` },
        el("td", { class: "sym", text: r.symbol }),
        ...r.p.map((x) => el("td", { class: cls(x), text: fmtPct(x) })),
        el("td", {}, dirTag(r.d))))));
  }

  function renderScanner() {
    const lists = Object.keys(SCAN).map(renderScanList);
    renderCommon(lists);
  }

  function render() {
    renderedOnce = true;
    document.querySelectorAll(".view-btn").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === state.view)));
    $("scannerView").hidden = state.view !== "scanner";
    $("detailsView").hidden = state.view !== "details";
    if (state.view === "scanner") {
      renderBanner(state.data.live, "Live");
      renderScanner();
      return;
    }
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === state.tab)));
    renderBanner(state.data[state.tab]);
    renderSectorFilter();
    renderSectors();
    renderStocks();
  }

  // -------------------------------------------------------------- events --
  document.querySelectorAll(".view-btn").forEach((b) => b.addEventListener("click", () => {
    state.view = b.dataset.view;
    store.set("fno_view", state.view);
    render();
    refresh();
  }));
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    state.tab = t.dataset.tab;
    store.set("fno_tab", state.tab);
    render();
    refresh();
  }));
  $("search").addEventListener("input", (e) => { state.search = e.target.value; renderStocks(); });
  $("sectorFilter").addEventListener("change", (e) => { state.sector = e.target.value; render(); });
  $("hlOnly").addEventListener("change", (e) => { state.hlOnly = e.target.checked; renderStocks(); });
  $("daySelect").addEventListener("change", (e) => {
    state.day = e.target.value;
    state.data = {};
    state.stale = {};
    state.etags = {};
    state.status = null;
    renderedOnce = false;
    schedule();
    refresh();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.day) refresh(); });

  const themes = ["auto", "light", "dark"];
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("themeBtn").title = `Theme: ${t}`;
  }
  let theme = store.get("fno_theme") || "auto";
  applyTheme(theme);
  $("themeBtn").addEventListener("click", () => {
    theme = themes[(themes.indexOf(theme) + 1) % themes.length];
    store.set("fno_theme", theme);
    applyTheme(theme);
  });

  async function loadDays() {
    try {
      const r = await getJSON("/api/snapshots");
      const sel = $("daySelect");
      const days = (r.body && r.body.snapshots) || [];
      sel.replaceChildren(el("option", { value: "", text: "Today" }),
        ...days.map((d) => el("option", { value: d.date, text: d.date })));
      sel.value = state.day;
    } catch (e) {
      if (e instanceof AuthError) askToken();
    }
  }

  render();
  refresh().then(loadDays);
  schedule();
})();
