// BotBowl Bench - shared helpers (no framework).
const BB = (() => {
  const api = (path) => fetch(path, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(r.status + " " + path);
    return r.json();
  });

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const fmt = {
    num: (v, d = 0) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })),
    pct: (v) => (v == null ? "–" : Math.round(v * 100) + "%"),
    usd: (v) => (v == null ? "–" : v < 0.01 && v > 0 ? "<$0.01" : "$" + Number(v).toFixed(2)),
    tokens: (v) => (v == null ? "–" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? Math.round(v / 1e3) + "k" : String(v)),
    ago: (t) => {
      if (!t) return "";
      const s = Date.now() / 1000 - t;
      if (s < 60) return "just now";
      if (s < 3600) return Math.floor(s / 60) + " min ago";
      if (s < 86400) return Math.floor(s / 3600) + " h ago";
      return Math.floor(s / 86400) + " d ago";
    },
    dur: (s) => (s == null ? "–" : s < 60 ? Math.round(s) + "s" : Math.floor(s / 60) + "m " + Math.round(s % 60) + "s"),
  };

  // Stable categorical colour per model: by the order models were added, never by rank.
  const SERIES = 8;
  let colorIndex = {};
  function setModelOrder(models) {
    const sorted = [...models].sort((a, b) => (a.added_at || 0) - (b.added_at || 0) || a.id.localeCompare(b.id));
    colorIndex = {};
    sorted.forEach((m, i) => (colorIndex[m.id] = i));
  }
  const modelColor = (id) => {
    const i = colorIndex[id];
    return i == null || i >= SERIES ? "var(--text-muted)" : `var(--series-${i + 1})`;
  };

  function matchRow(m) {
    let score = "vs", tag = h("span", { class: "badge tag" }, m.status);
    if (m.status === "completed") { score = `${m.home_score} – ${m.away_score}`; tag = h("span", { class: "muted tag" }, fmt.ago(m.finished_at)); }
    else if (m.status === "running") { score = m.home_score != null ? `${m.home_score} – ${m.away_score}` : "vs"; tag = h("span", { class: "badge live tag" }, "LIVE"); }
    const hw = m.winner === "home", aw = m.winner === "away";
    return h("a", { class: "match-row", href: `/match/${m.id}` },
      h("span", { class: "home", style: { fontWeight: hw ? 700 : 400 } }, m.home_name),
      h("span", { class: "score" }, score),
      h("span", { class: "away", style: { fontWeight: aw ? 700 : 400 } }, m.away_name),
      tag);
  }

  function statusPill() {
    const pill = document.getElementById("status-pill");
    if (!pill) return;
    const update = () => api("/api/status").then((s) => {
      pill.innerHTML = "";
      let cls = "idle", txt = s.detail || s.status;
      if (s.live.length) { cls = "live"; const m = s.live[0]; txt = `Live: ${m.home_name} vs ${m.away_name}`; }
      else if (s.status === "waiting" || s.status === "paused" || s.status === "error") cls = "wait";
      pill.append(h("span", { class: "dot " + cls }), h("span", { class: "txt" }, txt));
      if (s.live.length) pill.onclick = () => (location.href = `/match/${s.live[0].id}`), (pill.style.cursor = "pointer");
    }).catch(() => {});
    update();
    setInterval(update, 10000);
  }

  function cellbar(value, max, text) {
    const w = max > 0 ? Math.max(0, Math.min(1, value / max)) * 100 : 0;
    return h("div", { class: "cellbar" }, h("span", {}, text),
      h("span", { class: "track" }, h("span", { class: "fill", style: { width: w + "%", display: "block" } })));
  }

  // Multi-series line chart (Elo over matches) with legend toggles and a crosshair tooltip.
  function lineChart(container, series, opts = {}) {
    container.innerHTML = "";
    container.classList.add("chart");
    const W = 760, H = 280, P = { l: 44, r: 12, t: 10, b: 26 };
    const hidden = new Set();
    const tip = h("div", { class: "tooltip" });
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", opts.label || "chart");
    container.append(svg, tip);
    const legend = h("div", { class: "legend" });
    container.append(legend);
    const all = series.flatMap((s) => s.points);
    if (!all.length) { container.prepend(h("div", { class: "empty" }, "No completed matches yet.")); return; }
    const xmax = Math.max(...all.map((p) => p.x), 1);
    let ymin = Math.min(...all.map((p) => p.y)), ymax = Math.max(...all.map((p) => p.y));
    const pad = Math.max(20, (ymax - ymin) * 0.1); ymin = Math.floor((ymin - pad) / 10) * 10; ymax = Math.ceil((ymax + pad) / 10) * 10;
    const X = (x) => P.l + (x / xmax) * (W - P.l - P.r);
    const Y = (y) => P.t + (1 - (y - ymin) / (ymax - ymin)) * (H - P.t - P.b);
    const el = (n, a) => { const e = document.createElementNS(svgNS, n); for (const k in a) e.setAttribute(k, a[k]); return e; };
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = ymin + ((ymax - ymin) * i) / ticks;
      svg.append(el("line", { x1: P.l, x2: W - P.r, y1: Y(v), y2: Y(v), class: "gridline" }));
      const t = el("text", { x: P.l - 6, y: Y(v) + 4, "text-anchor": "end", class: "axis-label" }); t.textContent = Math.round(v); svg.append(t);
    }
    const xl = el("text", { x: W - P.r, y: H - 6, "text-anchor": "end", class: "axis-label" }); xl.textContent = opts.xLabel || ""; svg.append(xl);
    const paths = {};
    series.forEach((s) => {
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
      const path = el("path", { d, class: "series", stroke: s.color });
      svg.append(path); paths[s.id] = path;
      const btn = h("button", { type: "button", "aria-pressed": "true" }, h("span", { class: "swatch", style: { background: s.color } }), s.name);
      btn.onclick = () => {
        if (hidden.has(s.id)) hidden.delete(s.id); else hidden.add(s.id);
        btn.classList.toggle("off", hidden.has(s.id)); btn.setAttribute("aria-pressed", String(!hidden.has(s.id)));
        path.style.display = hidden.has(s.id) ? "none" : "";
      };
      legend.append(btn);
    });
    const cross = el("line", { y1: P.t, y2: H - P.b, stroke: "var(--text-muted)", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.append(cross);
    const hit = el("rect", { x: P.l, y: P.t, width: W - P.l - P.r, height: H - P.t - P.b, fill: "transparent" });
    svg.append(hit);
    hit.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const x = Math.round(((ev.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * xmax);
      cross.setAttribute("x1", X(x)); cross.setAttribute("x2", X(x)); cross.setAttribute("visibility", "visible");
      const rows = series.filter((s) => !hidden.has(s.id)).map((s) => {
        let last = null; for (const p of s.points) if (p.x <= x) last = p;
        return last ? { s, y: last.y } : null;
      }).filter(Boolean).sort((a, b) => b.y - a.y);
      tip.innerHTML = "";
      tip.append(h("div", { class: "muted" }, `${opts.xLabel || "x"} ${x}`));
      rows.forEach((r) => tip.append(h("div", { class: "row" }, h("span", {}, h("span", { class: "swatch", style: { background: r.s.color } }), r.s.name), h("b", {}, Math.round(r.y)))));
      tip.style.display = "block";
      const px = (ev.clientX - r.left), cw = container.clientWidth;
      tip.style.left = Math.min(px + 14, cw - tip.offsetWidth - 4) + "px"; tip.style.top = "8px";
    });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
  }

  return { api, h, fmt, matchRow, statusPill, cellbar, lineChart, setModelOrder, modelColor };
})();
document.addEventListener("DOMContentLoaded", BB.statusPill);
