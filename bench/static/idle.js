// BotBowl Bench - the homepage when no game is live (credits ran out, the tournament is over, or the bench is
// paused): a short plain-language status, a wall of small pitches each replaying a good past game, and podiums
// for the latest tournament and all time. Clicking a game opens a story panel under its row: a bigger board,
// key moments that jump into the replay, and both coaches' final game plans.
const BBIdle = (() => {
  const { h, api, fmt } = BB;
  const ALL_TIME_MIN_GAMES = 10;   // keeps a model with a handful of placement games off the all-time podium
  const WALL_SIZE = 6;

  const short = (t) => (t || "").split(":")[0];
  const sideName = (m, side) => (side === "home" ? m.home_name : m.away_name);
  const turnOf = (p) => (p.side === "home" ? p.ht : p.at);
  // the replay a few moves before frame i, so the build-up is visible
  const deep = (m, i) => `/match/${m.id}#f=${Math.max(0, i - 8)}`;

  function copy(kind, s, tour) {
    if (kind === "outage") return { badge: "PAUSED", cls: "paused", title: "Back shortly",
      body: "Live games are paused while the model budget is topped up. The interrupted game picks up from its last turn, so nothing is lost." };
    if (kind === "over") return { badge: "SEASON OVER", cls: "over", title: tour ? `${short(tour.name)}: complete` : "No games scheduled",
      body: "New games start automatically when a model is added to the bench." };
    return { badge: "NO GAME LIVE", cls: "paused", title: "No game in progress", body: "" };
  }

  // good games to show: real models on both sides, decisive, close and high scoring first, varied pairings
  const isBaseline = (n) => /baseline|scripted|random/i.test(n || "");
  async function classics(n) {
    const r = await api("/api/matches?admissible=1&result=decisive&limit=100");
    const ms = r.matches.filter((m) => !isBaseline(m.home_name) && !isBaseline(m.away_name) && m.frames > 150);
    const score = (m) => (m.home_score + m.away_score) * 2 - Math.abs(m.home_score - m.away_score) * 2.5;
    ms.sort((a, b) => score(b) - score(a));
    const seen = new Set(), out = [];
    for (const m of ms) {
      const key = [m.home_model, m.away_model].sort().join("|");
      if (seen.has(key)) continue;
      seen.add(key); out.push(m);
      if (out.length >= n) break;
    }
    return out;
  }

  async function timeline(id) {
    const p = (await api(`/api/matches/${id}/timeline`)).points, tds = [];
    p.forEach((x, i) => { const q = p[i - 1]; if (q && (x.hs > q.hs || x.as > q.as)) tds.push({ i, side: x.hs > q.hs ? "home" : "away", p: x }); });
    return { points: p, total: p.length, tds };
  }

  // touchdowns, second-half kick-off and full time, each a link into the replay
  function moments(m, tl) {
    const out = tl.tds.map((td) => ({ i: td.i, side: td.side, label: `${sideName(m, td.side)} scores`,
      sub: `H${td.p.half} T${turnOf(td.p)} · ${td.p.as}–${td.p.hs}` }));
    const ht = tl.points.findIndex((p) => p.half === 2);
    if (ht > 0) out.push({ i: ht, label: "Second half kick-off", sub: `${tl.points[ht].as}–${tl.points[ht].hs}` });
    out.push({ i: tl.total - 1, label: "Full time", sub: `${m.away_score}–${m.home_score}` });
    return out.sort((a, b) => a.i - b.i);
  }

  async function lastPlans(id) {
    let ev = [], after = 0;
    for (;;) {
      const b = await api(`/api/matches/${id}/events?after=${after}`);
      ev = ev.concat(b);
      if (b.length < 1000) break;
      after = b[b.length - 1].id;
    }
    const plans = {};
    ev.forEach((e) => { if (e.kind === "reflection" && e.side) plans[e.side] = e.payload.plan; });
    return plans;
  }

  // play recorded frames on a loop, waiting for each frame's movement; holds while the page is hidden or the
  // board is scrolled out of view, so an idle tab doesn't keep fetching frames
  function loop(board, el, id, tl, { from, speed }) {
    let i = from, dead = false, t = null, seen = true;
    const io = window.IntersectionObserver ? new IntersectionObserver((es) => { seen = es[0].isIntersecting; }) : null;
    if (io) io.observe(el);
    const step = () => {
      if (dead) return;
      if (document.hidden || !seen) { t = setTimeout(step, 1000); return; }
      board.frame(id, i, tl.total, speed).then((dur) => {
        if (dead) return;
        const end = i >= tl.total - 1;
        i = end ? 1 : i + 1;
        t = setTimeout(step, end ? 2500 : Math.max(dur || 0, 650 / speed));
      }).catch(() => { t = setTimeout(step, 3000); });
    };
    step();
    return { stop() { dead = true; clearTimeout(t); if (io) io.disconnect(); } };
  }
  const startNear = (tl, back) => Math.max(1, (tl.tds[0] ? tl.tds[0].i : 60) - back);

  // 2nd, 1st, 3rd left to right: name and figures above a stepped block with the place in it
  function podium(rows, name, href, figs) {
    return h("div", { class: "pd" }, ...rows.slice(0, 3).map((x, i) => h("div", { class: `pd-step s${i + 1}` },
      h("a", { class: "pd-nm", href: href(x) }, name(x)), h("span", { class: "pd-st" }, figs(x)),
      h("span", { class: "pd-block" }, ["1st", "2nd", "3rd"][i]))));
  }
  const resultRow = (m) => h("a", { href: `/match/${m.id}` },
    h("span", { class: `hn${m.winner === "away" ? " w" : ""}` }, m.away_name),
    h("span", { class: "sc" }, `${m.away_score}–${m.home_score}`),
    h("span", { class: m.winner === "home" ? "w" : "" }, m.home_name));

  function create(root) {
    let kind = null, gen = 0, loops = [], story = null, strip = null;

    function stop() {
      loops.forEach((l) => l.stop()); loops = [];
      if (story) { story.ctl && story.ctl.stop(); story = null; }
      kind = null; gen++;
    }

    function drawStrip(s, tour) {
      const c = copy(kind, s, tour);
      strip.replaceChildren(...[h("span", { class: `badge ${c.cls}` }, c.badge), h("h1", {}, c.title),
        c.body ? h("p", { class: "secondary" }, c.body) : null,
        kind !== "over" && s.detail ? h("details", { class: "why" }, h("summary", {}, "Technical detail"), h("code", {}, s.detail)) : null].filter(Boolean));
    }

    async function sidebar(side) {
      const [ts, lb] = await Promise.all([api("/api/tournaments"), api("/api/leaderboard")]);
      const t = ts.find((x) => x.status === "completed") || ts[0];
      const allTime = lb.models.filter((m) => m.played >= ALL_TIME_MIN_GAMES && !isBaseline(m.name));
      if (t) {
        const d = await api(`/api/tournaments/${t.id}`);
        const latest = (d.matches || []).filter((m) => m.status === "completed").sort((a, b) => b.finished_at - a.finished_at).slice(0, 5);
        side.append(
          h("div", { class: "pd-card" }, h("h2", {}, short(t.name), h("a", { href: `/tournaments/${t.id}` }, "Table →")),
            podium(d.standings, (x) => x.name, (x) => `/models/${x.model}`, (x) => `${x.points} pts`)),
          h("div", { class: "pd-card" }, h("h2", {}, "Latest results", h("a", { href: "/matches" }, "All →")),
            h("div", { class: "res" }, ...latest.map(resultRow))));
      }
      side.append(h("div", { class: "pd-card" }, h("h2", {}, "All-time best", h("a", { href: "/leaderboard" }, "Leaderboard →")),
        podium(allTime, (x) => x.name, (x) => `/models/${x.id}`, (x) => `${fmt.num(x.elo)} Elo`),
        h("div", { class: "pd-rest" }, ...allTime.slice(3, 8).map((x, i) => h("a", { href: `/models/${x.id}` },
          h("span", { class: "r" }, i + 4), h("span", {}, x.name), h("span", { class: "v" }, fmt.num(x.elo))))),
        h("p", { class: "muted pd-note" }, `Models with at least ${ALL_TIME_MIN_GAMES} games; baselines left out.`)));
    }

    async function wall(box) {
      const phone = matchMedia("(max-width: 640px)").matches, cols = phone ? 1 : 2;
      const g = gen, games = await classics(phone ? 3 : WALL_SIZE);
      if (g !== gen) return;
      const tiles = [], tls = [];

      async function openStory(n) {
        const wasOpen = story && story.n === n;
        if (story) { story.ctl && story.ctl.stop(); story.el.remove(); tiles.forEach((t) => t.classList.remove("open")); story = null; }
        if (wasOpen) return;
        const m = games[n], tl = await tls[n];
        const bEl = h("div"), side = h("div", { class: "story-side" });
        story = { n, el: h("div", { class: "story" }, bEl, side), ctl: null };
        tiles[n].classList.add("open");
        tiles[Math.min(tiles.length - 1, Math.floor(n / cols) * cols + cols - 1)].after(story.el);
        const plans = h("div", { class: "story-plans" }, h("h2", {}, "Final game plans"));
        side.append(
          h("h2", { class: "story-h" }, "Key moments", h("button", { type: "button", class: "story-x", "aria-label": "Close", onclick: () => openStory(n) }, "✕")),
          h("div", { class: "mom" }, ...moments(m, tl).map((x) => h("a", { href: deep(m, x.i), class: x.side || "" },
            h("span", { class: "t" }, "▶ jump"), h("span", {}, h("b", {}, x.label), h("small", {}, x.sub))))),
          plans,
          h("a", { class: "btn primary story-cta", href: `/match/${m.id}` }, "Open the match centre →"));
        lastPlans(m.id).then((p) => ["away", "home"].forEach((sd) => p[sd] &&
          plans.append(h("div", { class: `plan ${sd}` }, h("b", {}, `${sideName(m, sd)}: `), p[sd])))).catch(() => {});
        const board = BBBoard.create(bEl, { names: () => ({ home: m.home_name, away: m.away_name }),
          badge: () => h("span", { class: "badge replay" }, "REPLAY") });
        story.ctl = loop(board, bEl, m.id, tl, { from: startNear(tl, 20), speed: 3 });
        story.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }

      games.forEach((m, n) => {
        const el = h("div"), turn = h("span", { class: "wl-turn" });
        const t = h("a", { class: "wl-tile", href: `/match/${m.id}`, onclick: (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey) return;   // new tab still opens the match centre
          e.preventDefault(); openStory(n);
        } },
          h("div", { class: "wl-pitch" }, el, turn),
          h("div", { class: "wl-line" }, h("span", { class: "a" }, m.away_name), h("span", { class: "sc" }, `${m.away_score} – ${m.home_score}`), h("span", { class: "hm" }, m.home_name)),
          h("div", { class: "wl-meta" }, h("span", {}, short(m.tournament_name)), h("span", { class: "wl-watch" }, "Key moments ▾")));
        tiles.push(t); box.append(t);
        tls.push(timeline(m.id).then((tl) => {
          if (g !== gen) return tl;   // torn down while loading
          const board = BBBoard.create(el, { names: () => ({ home: m.home_name, away: m.away_name }) });
          const orig = board.frame;
          board.frame = (id, i, total, sp) => orig(id, i, total, sp).then((d) => {
            const p = tl.points[i];
            turn.textContent = `${p.as}–${p.hs} · ${p.side ? `H${p.half} T${turnOf(p)}` : "kick-off"}`;
            return d;
          });
          loops.push(loop(board, el, m.id, tl, { from: startNear(tl, 12 + Math.floor(Math.random() * 10)), speed: 3 }));
          return tl;
        }));
      });
    }

    // (re)build when the reason for being idle changes; otherwise only refresh the status line
    function show(s) {
      const k = BB.idleKind(s);
      if (k === kind) { drawStrip(s, strip._tour); return; }
      stop();
      kind = k;
      root.innerHTML = "";
      strip = h("div", { class: "wl-strip" });
      const wallBox = h("div", { class: "wl-grid" }), side = h("aside", { class: "idle-side" });
      root.append(strip, h("div", { class: "idle-split" },
        h("div", {}, h("div", { class: "wl-h" }, h("h2", {}, "Best games · click one for its story"), h("a", { href: "/matches" }, "All matches →")), wallBox),
        side));
      drawStrip(s, null);
      if (kind === "over") api("/api/tournaments").then((ts) => {
        strip._tour = ts.find((x) => x.status === "completed") || null;
        if (kind === "over") drawStrip(s, strip._tour);
      }).catch(() => {});
      wall(wallBox).catch(() => wallBox.append(h("div", { class: "empty" }, "No replays to show yet.")));
      sidebar(side).catch(() => {});
    }

    return { show, stop };
  }

  return { create };
})();
