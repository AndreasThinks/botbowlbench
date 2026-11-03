// About page: section rail, in-page focus, the illustrative request card, the live health summary (real data) and
// the illustrative board.
(() => {
  const { h, api } = BB;
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // in-page links scroll and move focus to the target (open it if it is a <details>)
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest('.about a[href^="#"]');
    if (!a) return;
    const t = document.getElementById(a.getAttribute("href").slice(1));
    if (!t) return;
    e.preventDefault();
    if (t.tagName === "DETAILS") t.open = true;
    history.replaceState(null, "", "#" + t.id);
    t.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    if (!t.hasAttribute("tabindex")) t.setAttribute("tabindex", "-1");
    t.focus({ preventScroll: true });
  });
  const opened = location.hash && document.getElementById(location.hash.slice(1));
  if (opened && opened.tagName === "DETAILS") opened.open = true;

  // the rail marks the section in view
  const links = [...document.querySelectorAll(".ab-rail a")];
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (!en.isIntersecting) return;
      links.forEach((l) => {
        const on = l.getAttribute("href") === "#" + en.target.id;
        l.classList.toggle("on", on);
        if (on) l.setAttribute("aria-current", "true"); else l.removeAttribute("aria-current");
      });
    }), { rootMargin: "-110px 0px -60% 0px" });
    links.forEach((l) => { const s = document.getElementById(l.getAttribute("href").slice(1)); if (s) io.observe(s); });
  }

  // ---- live: the real flags and holds, from /api/health -------------------------------------------------------
  const LABEL = { flagged: "flagged", clear: "clear", warming_up: "warming up", unmeasured: "not measured yet",
    not_applicable: "baselines (not applicable)" };
  const live = document.getElementById("ab-health-live");
  api("/api/health").then((d) => {
    const conds = Object.entries(d.conditions).filter(([, c]) => c.enabled || c.status === "flagged" || c.hold);
    const counts = {};
    conds.forEach(([, c]) => (counts[c.status] = (counts[c.status] || 0) + 1));
    const order = ["flagged", "clear", "warming_up", "unmeasured", "not_applicable"];
    const parts = [h("ul", { class: "ab-counts", "aria-label": "Conditions by health status" },
      ...order.filter((k) => counts[k]).map((k) => h("li", {}, h("b", {}, counts[k]), " ", LABEL[k])))];
    const flagged = conds.filter(([, c]) => c.flags.length || c.hold);
    if (flagged.length) {
      parts.push(h("ul", { class: "ab-flagged" }, ...flagged.map(([id, c]) => h("li", {},
        h("a", { href: `/models/${id}#health` }, c.name), ...BBHealth.chips({ id, health: c })))));
    } else {
      parts.push(h("p", { class: "muted" }, "No condition is flagged or on hold."));
    }
    if (!counts.flagged && !counts.clear && !counts.warming_up) {
      parts.push(h("p", { class: "muted" }, "No game has been measured with the health counters yet: every condition reads as not measured, which means unknown, not healthy."));
    }
    live.replaceChildren(...parts);
  }).catch(() => live.replaceChildren(h("p", { class: "muted" }, "The current flags could not be loaded. Each model page shows its own.")));

  // ---- illustrative request card: two independent controls, changing only this example ----------------------------
  const rq = document.getElementById("ab-rq");
  if (rq) {
    const st = { tc: "required", moment: "ordinary" };
    const $ = (sel) => rq.querySelector(sel);
    const fmt = (n) => Number(n).toLocaleString("en");
    const reply = rq.dataset.reply, reserve = rq.dataset.reserve;
    const when = [reply && `a reply still running at ${reply} s`, reserve && `the last ${reserve} s of a turn`].filter(Boolean).join(" or ");
    const tag = (el, cls, text) => { el.className = "ab-rq-tag" + (cls ? " " + cls : ""); el.textContent = text; };
    const STATUS = {
      "required-ordinary": ["House default.", " Reasoning and a required tool call, in one request. Conditions not listed under exceptions start every game with exactly this request."],
      "auto-ordinary": ["Compatibility exception.", " Used by Claude models, the conditions listed under exceptions with automatic tool choice, and any seat after a compatibility retry. Only the tool-call requirement changed: reasoning is still requested, exactly as before."],
      "required-short": ["Declared timing exception.", ` After ${when}: the lowest reasoning effort the model lists and at most ${fmt(rq.dataset.shortMax)} output tokens, so it can still make its own move. Tool choice is unchanged.`],
      "auto-short": ["Both exceptions at once.", " A condition with automatic tool choice near a time limit: the tool call is optional and the reply is short. The short reply is what reduces reasoning, not auto."],
    };
    const render = () => {
      const auto = st.tc === "auto", short = st.moment === "short";
      const tl = $('[data-rq="tool_choice"]'), rl = $('[data-rq="reasoning"]'), mt = $('[data-rq="max_tokens"]');
      tl.textContent = `"tool_choice": "${st.tc}"`;
      tl.classList.toggle("changed", auto);
      rl.textContent = short ? '"reasoning": {"effort": "<lowest the model lists>"}' : '"reasoning": {"enabled": true}';
      rl.classList.toggle("changed", short);
      if (mt) {
        mt.textContent = `"max_tokens": ${short ? rq.dataset.shortMax : rq.dataset.max}`;
        mt.classList.toggle("changed", short);
      }
      $(".ab-rq-ph").hidden = !short;
      tag($("#rq-tag-tools"), auto ? "changed" : (short ? "same" : ""), auto ? "changed: tool call optional" : (short ? "unchanged" : "house default"));
      tag($("#rq-tag-reasoning"), short ? "changed" : (auto ? "same" : ""),
        short ? "reduced: declared timing exception" : (auto ? "unchanged: still requested" : "house default"));
      const r1 = $('[data-rq="reply-reasoning"]'), r2 = $('[data-rq="reply-tool"]');
      r1.textContent = short ? "Little or no reasoning, by request" : "Reasoning, as far as the provider returns it";
      r1.classList.toggle("changed", short);
      r2.textContent = auto ? "A tool call, or prose only (prose moves no player and counts as an invalid call)"
        : "A tool call, required: look, chat, note or game action";
      r2.classList.toggle("changed", auto);
      const [b, rest] = STATUS[`${st.tc}-${st.moment}`];
      const s = $("#rq-status");
      s.replaceChildren(h("b", {}, b), rest);
      s.dataset.mode = `${st.tc}-${st.moment}`;
      rq.querySelectorAll("[data-rq-tc]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.rqTc === st.tc)));
      rq.querySelectorAll("[data-rq-moment]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.rqMoment === st.moment)));
    };
    rq.querySelectorAll("[data-rq-tc]").forEach((x) => x.addEventListener("click", () => { st.tc = x.dataset.rqTc; render(); }));
    rq.querySelectorAll("[data-rq-moment]").forEach((x) => x.addEventListener("click", () => { st.moment = x.dataset.rqMoment; render(); }));
    render();
  }

  // ---- illustrative board: made-up numbers, real thresholds -------------------------------------------------------
  const board = document.getElementById("ab-board");
  if (!board) return;
  const TH = { R: +board.dataset.r, P: +board.dataset.p, L: +board.dataset.l, S: +board.dataset.s };
  const S_GAMES = +board.dataset.sGames;
  const LABELS = { R: "Low-reasoning team-turn replies (first window)", P: "Replies without a tool call",
    L: "Replies cut off at the output limit", S: `Games with provider fallback (of the latest ${S_GAMES})` };
  const SC = {
    healthy: { hosts: [{ n: "Host A", share: 70, R: 3, P: 1, L: 0, S: 0 }, { n: "Host B", share: 30, R: 5, P: 2, L: 1, S: 0 }],
      verdict: "No flag. The placement carries on.",
      explain: "Every signal is under its threshold on every host. The model's games count like anyone else's, wins and losses alike.",
      changes: "Settings: unchanged · Rating: unaffected · Maintainer action: none" },
    reasoning: { hosts: [{ n: "Host A", share: 60, R: 2, P: 1, L: 1, S: 0 }, { n: "Host B", share: 40, R: 100, P: 0, L: 0, S: 0 }],
      verdict: "Flag R on Host B. A successor may be suggested.",
      explain: "Reasoning was requested, but Host B returned almost none on team-turn replies. That points at our own request, so the model page suggests one successor condition whose only change is automatic tool choice. A maintainer decides whether to add it; it would be rated separately with a fresh placement, and the original keeps its games.",
      avgExplain: "Averaged over both hosts the share stays under the threshold, so nothing would fire. This is why every signal is read per host.",
      changes: "Suggested change: tool_choice required to auto, nothing else · New competitor ID · Old games kept" },
    prose: { hosts: [{ n: "Host A", share: 100, R: 4, P: 9, L: 1, S: 0 }],
      verdict: "Flag P. No settings change.",
      explain: "Replying in prose instead of acting is part of what the benchmark measures. The harness already answers each such reply with a nudge to call a tool and counts it towards the invalid-call limit.",
      changes: "Settings: unchanged · Shown on the leaderboard and model page" },
    cap: { hosts: [{ n: "Host A", share: 55, R: 2, P: 1, L: 7, S: 0 }, { n: "Host B", share: 45, R: 3, P: 0, L: 6, S: 0 }],
      verdict: "Flag L. The ceiling stays where it is.",
      explain: "The model keeps generating past the shared output ceiling before it acts. That ceiling is the same for everyone and is not raised for one entrant. Long output is not evidence of better play.",
      changes: "Settings: unchanged · Truncations shown apart from missing reasoning" },
    outage: { hosts: [{ n: "Host A", share: 100, R: 3, P: 1, L: 0, S: 2 }],
      verdict: "Flag S. The condition goes on hold.",
      explain: "Provider errors handed decisions to the default policy in two of the latest three games. The game in progress finishes, the condition's queued games keep their place and wait, and other games carry on until a maintainer releases the hold.",
      changes: "Settings: unchanged · Same competitor ID · Held, not retired or disabled" },
  };
  let view = "host";
  const tbody = board.querySelector("#ab-meters tbody");
  const rowsFor = (sc) => {
    if (view === "host" || sc.hosts.length === 1) return sc.hosts;
    const avg = { n: "All hosts (average)", share: 100, R: 0, P: 0, L: 0, S: 0 };
    sc.hosts.forEach((x) => { ["R", "P", "L"].forEach((k) => (avg[k] += (x[k] * x.share) / 100)); avg.S = Math.max(avg.S, x.S); });
    ["R", "P", "L"].forEach((k) => (avg[k] = Math.round(avg[k] * 10) / 10));
    return [avg];
  };
  function render() {
    const sc = SC[board.querySelector('input[name="scn"]:checked').value];
    const on = {};
    const rows = [];
    rowsFor(sc).forEach((x) => ["R", "P", "L", "S"].forEach((k, i) => {
      const max = k === "R" ? 100 : k === "S" ? S_GAMES : 12;
      const v = x[k], over = k === "S" ? v >= TH.S : k === "R" ? v >= TH.R : v > TH[k];
      if (over) on[k] = true;
      rows.push(h("tr", { class: i === 0 ? "first" : null },
        i === 0 ? h("th", { scope: "row", rowspan: 4 }, x.n, view === "host" ? h("span", { class: "sub" }, `${x.share}% of calls`) : null) : null,
        h("td", {}, LABELS[k]),
        h("td", {}, h("div", { class: "ab-bar", "aria-hidden": "true" },
          h("i", { class: over ? "over" : null, style: { width: Math.min(100, (v / max) * 100) + "%" } }),
          h("u", { style: { left: (TH[k] / max) * 100 + "%" } }))),
        h("td", { class: "num" }, k === "S" ? `${v}/${S_GAMES}` : `${v}%`, over ? " (over)" : "")));
    }));
    tbody.replaceChildren(...rows);
    board.querySelectorAll("#ab-lamps li").forEach((l) => {
      const k = l.dataset.k;
      if (k === "H") { l.setAttribute("aria-label", "H: host mix, always shown"); return; }
      l.classList.toggle("on", !!on[k]);
      l.setAttribute("aria-label", `${k}: ${on[k] ? "fired" : "quiet"}`);
    });
    const hidden = view === "avg" && sc.avgExplain && !on.R;
    board.querySelector("#ab-verdict").textContent = hidden ? "No flag in the average. The problem is hidden." : sc.verdict;
    board.querySelector("#ab-explain").textContent = hidden ? sc.avgExplain : sc.explain;
    board.querySelector("#ab-changes").textContent = hidden ? "Switch back to Per host to see what would happen." : sc.changes;
  }
  board.querySelectorAll('input[name="scn"]').forEach((r) => r.addEventListener("change", render));
  board.querySelectorAll(".ab-seg button").forEach((b) => b.addEventListener("click", () => {
    view = b.dataset.view;
    board.querySelectorAll(".ab-seg button").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
    render();
  }));
  render();
})();
