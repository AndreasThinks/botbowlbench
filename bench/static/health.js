// Tripwire condition health (see bench/health.py): leaderboard chips and the model page card. No framework.
// Flags describe how a condition's serving route behaved, never how well it played.
const BBHealth = (() => {
  const { h, fmt } = BB;
  const SHORT = { R: "reasoning missing", P: "prose replies", L: "output cap", S: "substituted play" };
  const NAME = { R: "Reasoning not returned", P: "Prose instead of tools", L: "Output cap reached", S: "Substituted play" };
  const STATE = {
    flagged: "Flagged", clear: "Clear", warming_up: "Warming up", insufficient_data: "Not enough replies",
    not_applicable: "Not applicable", unknown: "Not measured", unmeasured: "Not measured",
  };
  const date = (t) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "");
  const pct1 = (v) => (v == null ? "–" : (v * 100).toFixed(1) + "%");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  // the flag chips after a model's name: text first, so nothing depends on colour
  function chips(model) {
    const hl = model.health;
    if (!hl) return [];
    const out = hl.flags.map((k) => {
      const hosts = (hl.flagged_hosts || {})[k] || [];
      const where = hosts.length ? ` on ${hosts.join(", ")}` : "";
      const label = `Health flag ${k}: ${NAME[k]}${where}. Open the condition health details.`;
      return h("a", { class: "hflag", href: `/models/${model.id}#health`, title: label, "aria-label": label },
        h("b", {}, k), " ", SHORT[k]);
    });
    if (hl.hold) {
      const label = "On health hold: new games involving this condition wait until a maintainer releases the hold.";
      out.push(h("a", { class: "hflag hold", href: `/models/${model.id}#health`, title: label, "aria-label": label }, "on hold"));
    }
    return out;
  }

  function stateTag(state) {
    return h("span", { class: `hstate s-${state}` }, STATE[state] || state);
  }

  function hostTable(head, rows) {
    if (!rows.length) return null;
    return h("div", { class: "table-wrap" }, h("table", { class: "htable" },
      h("thead", {}, h("tr", {}, ...head.map((x, i) => h("th", { class: i ? "num" : "" }, x)))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((x, i) => h("td", { class: i ? "num" : "" }, x)))))));
  }

  function signalBody(k, s, t, hl) {
    if (s.state === "unknown") return h("p", { class: "muted" }, "Not measured: no instrumented games yet.");
    if (s.state === "not_applicable") return h("p", { class: "muted" }, s.reason ? `Not applicable: ${s.reason}.` : "Not applicable.");
    if (k === "R") {
      return [h("p", { class: "hnote" }, `${s.window_note}. Flag at ${Math.round(t.R.low_share * 100)}% or more low-evidence replies once a host has ${t.R.window}.`),
        hostTable(["Host", "Replies counted", "Low evidence", "State"],
          s.hosts.map((x) => [x.host, `${x.eligible} / ${t.R.window}`, x.share == null ? "–" : `${x.low} (${fmt.pct(x.share)})`, stateTag(x.state)]))];
    }
    if (k === "P" || k === "L") {
      const what = k === "P" ? "with no tool call" : "cut off at the output limit";
      const summary = s.state === "warming_up"
        ? `${plural(s.games_seen, "instrumented game")} of the ${t[k].games} needed so far.`
        : `${s.count} of ${plural(s.responses, "reply")} ${what} over the latest ${s.games_seen} games (${pct1(s.rate)}). Flag above ${Math.round(t[k].rate * 100)}%.`;
      const extra = k === "P"
        ? `Counted apart: ${s.bad_args} tool calls with unreadable arguments (of ${s.tool_calls}), ${s.reflection_count} of ${s.reflection_responses} reflection replies.`
        : `Requested output ceiling: ${s.requested_max_tokens ? fmt.num(s.requested_max_tokens) + " tokens" : "provider default"}. Where a host cut replies off, the token counts it stopped at are shown: below the requested ceiling, the host's own limit was lower.`;
      return [h("p", { class: "hnote" }, summary, " ", extra),
        hostTable(k === "P" ? ["Host", "Replies", "No tool call", "State"] : ["Host", "Replies", "Cut off", "Stopped at", "State"],
          s.hosts.map((x) => {
            const row = [x.host, x.responses, `${x.count} (${pct1(x.rate)})`];
            if (k === "L") row.push(x.truncated_at ? (x.truncated_at[0] === x.truncated_at[1] ? fmt.num(x.truncated_at[0]) : `${fmt.num(x.truncated_at[0])} to ${fmt.num(x.truncated_at[1])}`) : "–");
            row.push(x.state === "insufficient_data" ? h("span", { class: "muted" }, `fewer than ${t[k].min_host_responses}`) : stateTag(x.state));
            return row;
          }))];
    }
    const causes = hl.fallback_causes || {};
    return [h("p", { class: "hnote" }, `Games where the default policy played because the provider failed: ${s.games_with} of the latest ${s.games_seen}. Flag at ${t.S.games_with} of ${t.S.games}. The failed host is shown only when the error named it.`),
      hostTable(["Game", "Provider fallback", "Failed calls (host: kind)", "Other fallback"], s.games.map((g) => [
        h("a", { href: `/match/${g.match_id}` }, date(g.finished_at) || g.match_id.slice(0, 8)),
        g.provider_fallback_actions ? `${g.provider_fallback_actions} decisions` : "none",
        Object.entries(g.errors).map(([host, kinds]) => `${host}: ${Object.entries(kinds).map(([k2, n]) => `${n} ${k2.replace(/_/g, " ")}`).join(", ")}`).join("; ") || "–",
        Object.entries(g.fallback).filter(([c]) => c !== "llm_error" && c !== "unavailable")
          .map(([c, n]) => h("span", { title: causes[c] || c }, `${n} ${c.replace(/_/g, " ")}`)).flatMap((x, i) => (i ? [", ", x] : [x])),
      ]))];
  }

  function headline(hl) {
    switch (hl.status) {
      case "flagged": return `Flagged: ${hl.flags.map((k) => `${k} (${NAME[k].toLowerCase()})`).join(", ")}.`;
      case "clear": return "Clear: every applicable signal measured and under its threshold.";
      case "warming_up": return "Warming up: not enough measured replies or games for every signal yet.";
      case "unmeasured": return "Not measured: every game of this condition predates the health counters. That is unknown, not healthy.";
      case "not_applicable": return "Not applicable: baselines make no model calls.";
      default: return hl.status;
    }
  }

  function card(el, hl, model) {
    const t = hl.thresholds, text = hl.text || {};
    const cov = hl.coverage;
    const parts = [h("p", { class: "hhead" }, stateTag(hl.status), " ", headline(hl))];
    if (hl.hold) {
      const how = hl.hold.placed_by === "auto" ? "placed automatically by signal S" : "placed by a maintainer";
      parts.push(h("div", { class: "hhold", role: "note" }, h("b", {}, "On hold"), ` since ${date(hl.hold.placed_at)} (${how}). `,
        "The game in progress was allowed to finish. Queued games involving this condition keep their place and wait; everything else carries on. It stays listed and enabled; a maintainer releases the hold."));
    }
    if (hl.status !== "not_applicable") {
      parts.push(h("p", { class: "muted hcov" }, cov.games
        ? `${cov.instrumented} of ${plural(cov.games, "completed game")} measured${hl.since ? ` (first on ${date(hl.since)})` : ""}.` +
          (cov.legacy ? ` ${plural(cov.legacy, "earlier game")} predate the health counters and are shown as not measured.` : "") +
          (cov.partial ? ` ${plural(cov.partial, "game")} resumed from an older checkpoint and are left out of the windows.` : "")
        : "No completed games yet."));
      const rows = ["R", "P", "L", "S"].map((k) => {
        const s = hl.signals[k];
        return h("details", { class: "hsig", open: s.state === "flagged" ? true : null },
          h("summary", {}, h("span", { class: "hkey" }, k), h("span", { class: "hname" }, NAME[k]), stateTag(s.state)),
          h("div", { class: "hsig-body" }, signalBody(k, s, t, hl),
            text[k] ? h("p", { class: "muted hnext" }, h("b", {}, "What happens next: "), text[k].next) : null));
      });
      parts.push(h("div", { class: "hsigs" }, ...rows));
    }
    // H: always shown, measured or not
    if (hl.hosts.length) {
      parts.push(h("h3", { class: "hsub" }, "H · Host mix"), h("p", { class: "muted hnote" },
        "Share of model calls each serving host answered, over every game of this condition (recorded since before the other counters)."),
        h("ul", { class: "hmix" }, ...hl.hosts.map((x) => h("li", {},
          h("span", { class: "hmix-name" }, x.host),
          h("span", { class: "hmix-bar", "aria-hidden": "true" }, h("span", { style: { width: (x.share || 0) * 100 + "%" } })),
          h("span", { class: "num" }, `${fmt.pct(x.share)} · ${fmt.num(x.calls)} calls`)))));
    }
    if (hl.successor) {
      const s = hl.successor;
      parts.push(h("div", { class: "hsucc" }, h("h3", { class: "hsub" }, "Suggested successor (not applied)"), h("p", {}, s.note),
        s.change ? h("pre", { class: "hyaml" }, `- name: ${model.name} (auto tools)\n  id: ${s.suggested_id}\n  model: ${model.model}\n  tool_choice: auto`) : null));
    }
    parts.push(h("p", { class: "muted hnote" }, "These signals describe the serving route, not playing strength. ",
      h("a", { href: "/about#health" }, "How the health checks work"), " · ", h("a", { href: "/api/health" }, "rules as JSON")));
    el.replaceChildren(...parts);
  }

  return { chips, card, SHORT, NAME };
})();
