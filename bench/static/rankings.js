// Alternative leaderboard rankings, computed from the /api/leaderboard payload (each model's summed stats).
// Every model gets the same formula. Rankings about model calls (tool calling, value, speed) leave out the scripted
// and random baselines, which make no LLM calls, and only LLMs with MIN_GAMES or more games are ranked.
const BBRank = (() => {
  const { fmt } = BB;
  const MIN_GAMES = 8;
  const isLLM = (m) => m.provider === "openrouter";
  const S = (m, k) => (m.sums && m.sums[k]) || 0;
  const perTurn = (m, k) => S(m, k) / Math.max(1, S(m, "turns"));
  const perGame = (m, k) => S(m, k) / Math.max(1, m.played);

  // share of the other values in the pool this one beats (ties count half), 0..1
  function percentile(v, pool, better) {
    if (v == null) return null;
    const vals = pool.filter((x) => x != null);
    if (vals.length < 2) return 1;
    let s = 0;
    for (const x of vals) s += (better === "high" ? v > x : v < x) ? 1 : v === x ? 0.5 : 0;
    return Math.max(0, Math.min(1, (s - 0.5) / (vals.length - 1)));
  }

  // the four measures behind the decision-making score
  const DECISION_PARTS = [
    { label: "safe-first", v: (m) => m.style.safe_first_rate, better: "high", f: fmt.pct },
    { label: "risky success", v: (m) => m.style.avg_risky_success, better: "high", f: fmt.pct },
    { label: "turnovers / turn", v: (m) => (S(m, "turns") ? perTurn(m, "turnovers") : null), better: "low", f: (v) => fmt.num(v, 2) },
    { label: "idle at turnover", v: (m) => m.style.unactivated_at_turnover, better: "low", f: (v) => fmt.num(v, 1) },
  ];

  const RANKINGS = [
    { id: "overall", label: "Overall", head: "Elo" },
    { id: "tool-calling", label: "Tool calling", head: "Clean calls", better: "high", applies: isLLM,
      lede: "Who drives the tools cleanly: the share of model replies that came back as a valid tool call.",
      how: "Clean calls = 1 − (invalid tool calls + replies with no tool call + replies cut off at the output length limit) ÷ model calls. " +
        "Provider errors, HTTP timeouts and content-filter retries are the host's fault, not the model's, and are left out. " +
        "Harness moves: actions the harness had to make for the model (time limit, call budget or too many invalid calls).",
      value: (m) => { const c = S(m, "llm_calls"); return c ? 1 - (S(m, "invalid_tool_calls") + S(m, "no_tool_replies") + S(m, "output_truncations")) / c : null; },
      fmt: (v) => (v * 100).toFixed(1) + "%",
      strip: (m) => [["invalid", fmt.num(perGame(m, "invalid_tool_calls"), 1) + "/g"], ["cut off", fmt.num(perGame(m, "output_truncations"), 1) + "/g"],
        ["harness moves", fmt.num(perGame(m, "forced_actions"), 1) + "/g"]] },
    { id: "decisions", label: "Decision-making", head: "Score", better: "high", applies: (m) => m.style && m.style.safe_first_rate != null,
      lede: "Who plays sound Blood Bowl, win or lose: safe actions first, good odds, few turnovers and no players wasted.",
      how: "Score 0–100 = average percentile among the ranked LLMs on four measures: safe-first rate (how often an action is at least as safe as every later one that turn), " +
        "average success chance of the actions that could fail, turnovers per turn (fewer is better) and players left unactivated when a turnover ended the turn (fewer is better). " +
        "Because it compares models with each other, scores shift a little when a model joins.",
      value: (m) => m._decisions, fmt: (v) => fmt.num(v),
      strip: (m) => DECISION_PARTS.map((p) => [p.label, p.v(m) == null ? "–" : p.f(p.v(m))]) },
    { id: "value", label: "Value", head: "Per win", better: "low", applies: isLLM,
      lede: "What a win costs: total spend divided by games won. Cheap models that also win rank highest.",
      how: "Cost per win = total OpenRouter cost ÷ (wins + half the draws). Models on the price/Elo frontier, where no model is both cheaper per game and rated higher, are marked. " +
        "A model on a free endpoint costs $0.00.",
      value: (m) => { const w = m.wins + m.draws / 2; return w ? S(m, "cost") / w : null; },
      fmt: (v) => fmt.usd(v), empty: "no wins",
      strip: (m) => [["per game", fmt.usd(m.avg_cost)], ["tokens / game", fmt.tokens(m.avg_tokens)]] },
    { id: "speed", label: "Speed", head: "Per turn", better: "low", applies: isLLM,
      lede: "Who keeps the game moving: model time per team turn, and how often the turn clock ran out.",
      how: "Seconds per turn = total model response time ÷ team turns played. Clock-outs = model calls cancelled at the turn deadline, per game.",
      value: (m) => (S(m, "turns") ? S(m, "latency") / S(m, "turns") : null),
      fmt: (v) => Math.round(v) + "s",
      strip: (m) => [["clock-outs", fmt.num(perGame(m, "deadline_cutoffs"), 1) + "/g"], ["s / call", m.avg_latency ? fmt.num(m.avg_latency, 1) : "–"],
        ["thinking tok / turn", fmt.tokens(Math.round(perTurn(m, "reasoning_tokens")))]] },
  ];

  const eligible = (m) => isLLM(m) && m.played >= MIN_GAMES;

  // { ranked: [{m, v, rank, pct, eloPos}], rest: [{m, v}] } for one of the non-Elo rankings
  function rank(models, r) {
    const played = models.filter((m) => m.played);
    const pool = played.filter(eligible);
    const parts = DECISION_PARTS.map((p) => pool.map(p.v));
    for (const m of played) {
      const ps = DECISION_PARTS.map((p, i) => percentile(p.v(m), parts[i], p.better)).filter((x) => x != null);
      m._decisions = ps.length ? Math.round((ps.reduce((a, b) => a + b, 0) / ps.length) * 100) : null;
    }
    const inRank = pool.filter(r.applies);
    const key = (m) => { const v = r.value(m); return v == null ? Infinity : r.better === "high" ? -v : v; };
    inRank.sort((a, b) => key(a) - key(b) || b.elo - a.elo);
    const vals = inRank.map(r.value);
    const byElo = [...inRank].sort((a, b) => b.elo - a.elo);
    const ranked = inRank.map((m, i) => ({ m, v: r.value(m), rank: i + 1, pct: percentile(r.value(m), vals, r.better), eloPos: byElo.indexOf(m) + 1,
      frontier: !pool.some((o) => o !== m && o.avg_cost <= m.avg_cost && o.elo > m.elo) }));
    // reference rows: newer models, and baselines where the measure means something for them
    const rest = played.filter((m) => !inRank.includes(m) && r.applies(m)).map((m) => ({ m, v: r.value(m) }));
    return { ranked, rest };
  }

  return { RANKINGS, MIN_GAMES, rank };
})();
