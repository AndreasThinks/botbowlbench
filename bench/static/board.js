// BotBowl Bench - a compact, responsive Blood Bowl board ("stadium") rendered from botbowl's game JSON.
// Reuses botbowl's own artwork (pitch images, player sprites) but none of its fixed-size layout.
const BBBoard = (() => {
  const IMG = "/static/img";
  // role -> sprite base name, copied from botbowl/web/static/js/services.js (IconService.playerIcons)
  const ICONS = {
            'Chaos': {
                'Beastman': 'cbeastman',
                'Chaos Warrior': 'cwarrior',
                'Minotaur': 'minotaur'
            },
            'Chaos Dwarf': {
                'Hobgoblin': 'cdhobgoblin',
                'Chaos Dwarf Blocker': 'cddwarf',
                'Bull Centaur': 'centaur',
                'Minotaur': 'minotaur'
            },
            'Dark Elf':{
                'Lineman': 'delineman',
                'Blitzer': 'deblitzer',
                'Witch Elf': 'dewitchelf',
                'Runner': 'dethrower',
                'Assassin': 'dehorkon'
            },
            'High Elf':{
                'Lineman': 'helineman',
                'Blitzer': 'heblitzer',
                'Thrower': 'hethrower',
                'Catcher': 'hecatcher'
            },
            'Wood Elf':{
                'Lineman': 'welineman',
                'Wardancer': 'weblitzer',
                'Thrower': 'wethrower',
                'Catcher': 'wecatcher',
                'Treeman': 'treeman'
            },
            'Human': {
                'Lineman': 'hlineman',
                'Blitzer': 'hblitzer',
                'Thrower': 'hthrower',
                'Catcher': 'hcatcher',
                'Ogre': 'ogre'
            },
            'Lizardman': {
                'Kroxigor': 'kroxigor',
                'Saurus': 'lmsaurus',
                'Skink': 'lmskink'
            },
            'Orc': {
                'Lineman': 'olineman',
                'Blitzer': 'oblitzer',
                'Thrower': 'othrower',
                'Black Orc Blocker': 'oblackorc',
                'Troll': 'troll',
                'Goblin': 'goblin'
            },
            'Elven Union': {
                'Lineman': 'eplineman',
                'Blitzer': 'epblitzer',
                'Thrower': 'epthrower',
                'Catcher': 'epcatcher'
            },
            'Skaven': {
                'Lineman': 'sklineman',
                'Blitzer': 'skstorm',
                'Thrower': 'skthrower',
                'Gutter Runner': 'skrunner',
                'Rat Ogre': 'ratogre'
            },
            'Amazon': {
                'Linewoman': 'amlineman',
                'Blitzer': 'amblitzer',
                'Thrower': 'amthrower',
                'Catcher': 'amcatcher'
            },
            'Undead': {
                'Zombie': 'uzombie',
                'Skeleton': 'uskeleton',
                'Ghoul': 'ughoul',
                'Wight': 'uwight',
                'Mummy': 'umummy'
            },
            'Vampire': {
                'Vampire': 'vampire',
                'Thrall': 'vthrall'
            }
        };
  const WEATHER = { NICE: "nice", VERY_SUNNY: "sunny", SWELTERING_HEAT: "heat", POURING_RAIN: "rain", BLIZZARD: "blizzard" };
  const h = (tag, cls, ...kids) => { const e = document.createElement(tag); if (cls) e.className = cls; kids.flat().forEach((k) => k != null && e.append(k)); return e; };
  const pretty = (s) => String(s || "").toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const DOING = { MOVE: "Moving", BLOCK: "Blocking", BLITZ: "Blitzing", PASS: "Passing", HANDOFF: "Handing off", FOUL: "Fouling", THROW_BOMB: "Throwing a bomb" };
  const TARGET_VERB = { BLOCK: "blocking", BLITZ: "blitzing", PASS: "passing to", HANDOFF: "handing off to", FOUL: "fouling" };

  function sprite(race, role, home, active) {
    const base = (ICONS[race] || {})[role] || "hlineman";
    return `${IMG}/iconssmall/${base}1${home ? "b" : ""}${active ? "an" : ""}.gif`;
  }

  function create(root, opts = {}) {
    root.classList.add("stadium");
    root.innerHTML = "";
    const head = h("div", "st-head");
    const pitchWrap = h("div", "st-pitch-wrap");
    const pitch = h("div", "st-pitch");
    const layer = h("div", "st-layer");
    pitch.append(layer);
    pitchWrap.append(pitch);
    const dugouts = h("div", "st-dugouts");
    const now = h("div", "st-now");
    now.setAttribute("aria-live", "polite");
    const ticker = h("ol", "st-ticker");
    ticker.setAttribute("aria-label", "Latest game events");
    const card = h("div", "st-card");
    card.hidden = true;
    root.append(head, pitchWrap, now, dugouts, ticker, card);
    let lastKey = null;
    let lastGame = null;

    // ---- player card: shown for the hovered player, or one pinned by click/tap; survives re-renders ----
    let hoverId = null, pinnedId = null;
    const touch = window.matchMedia && matchMedia("(hover: none)").matches;
    const pidOf = (t) => { const el = t && t.closest && t.closest("[data-pid]"); return el && root.contains(el) ? el.dataset.pid : null; };
    root.addEventListener("mouseover", (e) => { const id = card.contains(e.target) ? hoverId : pidOf(e.target); if (id !== hoverId) { hoverId = id; showCard(); } });
    root.addEventListener("mouseleave", () => { hoverId = null; showCard(); });
    root.addEventListener("click", (e) => {
      if (card.contains(e.target)) return;
      const id = pidOf(e.target);
      pinnedId = id && id !== pinnedId ? id : null;
      showCard();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pinnedId) { pinnedId = null; showCard(); } });

    function findPlayer(game, id) {
      for (const [side, team] of [["home", game.state.home_team], ["away", game.state.away_team]]) {
        if (team.players_by_id[id]) return { side, team, p: team.players_by_id[id] };
      }
      return null;
    }

    function where(game, side, p) {
      if (p.position) return null;
      const dug = side === "home" ? game.state.home_dugout : game.state.away_dugout;
      if (!dug) return "Off the pitch";
      if ((dug.casualties || []).includes(p.player_id)) return "Casualty";
      if ((dug.kod || []).includes(p.player_id)) return "Knocked out";
      if ((dug.dungeon || []).includes(p.player_id)) return "Sent off";
      return "In reserve";
    }

    // what the player is up to right now, in a few words
    function doing(game, side, p) {
      const st = game.state, s = p.state || {};
      const out = [];
      if (p.player_id === st.active_player_id) {
        out.push(DOING[st.player_action_type] || "Acting now");
        const other = game.active_other_player_id && findPlayer(game, game.active_other_player_id);
        const verb = TARGET_VERB[st.player_action_type];
        if (other && verb) out.push(`${verb} ${other.side === "home" ? "Home" : "Away"} #${other.p.nr}`);
      } else if (p.player_id === game.active_other_player_id) {
        const act = findPlayer(game, st.active_player_id);
        if (act) out.push(`Targeted by ${act.side === "home" ? "Home" : "Away"} #${act.p.nr}`);
      }
      const off = where(game, side, p);
      if (off) out.push(off);
      else if (s.stunned) out.push("Stunned");
      else if (!s.up) out.push("Prone");
      if (s.bone_headed) out.push("Bone-headed");
      if (s.really_stupid) out.push("Really stupid");
      if (s.hypnotized) out.push("Hypnotized");
      if (s.heated) out.push("Heat exhaustion");
      if (s.used && p.player_id !== st.active_player_id && !off) out.push("Already acted this turn");
      return out;
    }

    function cardBody(game, id) {
      const f = findPlayer(game, id);
      if (!f) return null;
      const { side, team, p } = f, s = p.state || {};
      const img = document.createElement("img");
      img.src = sprite(team.race, p.role, side === "home", false); img.alt = "";
      const moved = s.moves || 0;
      const maLeft = p.ma - moved;
      const stat = (label, val, note) => h("div", "st-stat", h("b", null, String(val)), h("span", null, label), note ? h("small", null, note) : null);
      const maNote = moved ? (maLeft >= 0 ? `${maLeft} left` : `${-maLeft} GFI`) : null;
      const skills = [...(p.role_skills || []), ...(p.extra_skills || [])];
      const status = doing(game, side, p);
      const ball = ((game.state.pitch && game.state.pitch.balls) || game.state.balls || [])
        .some((b) => b.is_carried && b.position && p.position && b.position.x === p.position.x && b.position.y === p.position.y);
      if (ball) status.unshift("Carrying the ball");
      return h("div", `st-card-in ${side}`,
        h("div", "st-card-top", img,
          h("div", "st-card-id",
            h("div", "st-card-name", `#${p.nr} ${p.name}`),
            h("div", "st-card-role", `${side === "home" ? "Home" : "Away"} · ${p.role}`))),
        h("div", "st-stats", stat("MA", p.ma, maNote), stat("ST", p.st), stat("AG", p.ag), stat("AV", p.av)),
        status.length ? h("div", "st-card-doing", status.join(" · ")) : null,
        skills.length ? h("div", "st-skills", skills.map((k) => h("span", "st-skill", pretty(k)))) : null,
        (p.injuries && p.injuries.length) ? h("div", "st-card-inj", "Injuries: " + p.injuries.map(pretty).join(", ")) : null,
        pinnedId === id ? h("div", "st-card-hint", touch ? "Pinned · tap the player again to close" : "Pinned · click again or press Esc to close") : null);
    }

    function showCard() {
      const id = hoverId || pinnedId;
      const body = id && lastGame ? cardBody(lastGame, id) : null;
      const anchor = id && root.querySelector(`[data-pid="${CSS.escape(id)}"]:not(.st-now)`);
      if (!body || !anchor) { card.hidden = true; card.innerHTML = ""; return; }
      card.innerHTML = ""; card.append(body); card.hidden = false;
      // sit beside the player, flipping to the other side near the edge, clamped inside the stadium
      const r = root.getBoundingClientRect(), a = anchor.getBoundingClientRect();
      const cw = card.offsetWidth, ch = card.offsetHeight, gap = 8;
      const clamp = (v, max) => Math.max(4, Math.min(max - 4, v));
      let left = a.right - r.left + gap, top = a.top - r.top + a.height / 2 - ch / 2;
      if (left + cw > r.width - 4) left = a.left - r.left - cw - gap;
      if (left < 4) {  // no room either side (narrow screens): centre it below the player instead
        left = a.left - r.left + a.width / 2 - cw / 2;
        top = a.bottom - r.top + gap;
        if (top + ch > r.height - 4) top = a.top - r.top - ch - gap;
      }
      left = clamp(left, r.width - cw); top = clamp(top, r.height - ch);
      card.style.left = `${left}px`; card.style.top = `${top}px`;
    }

    function teamHead(team, side, game, label) {
      const acting = game.state.current_team_id === team.team_id;
      const rr = h("span", "st-rerolls");
      for (let i = 0; i < (team.state.rerolls_start || 0); i++) rr.append(h("i", i < team.state.rerolls ? "on" : ""));
      rr.title = `Team re-rolls: ${team.state.rerolls} of ${team.state.rerolls_start} left`;
      const name = h("div", "st-name", label || team.name);
      return h("div", `st-team ${side} ${acting ? "acting" : ""}`, name,
        h("div", "st-sub", h("span", "st-tag", side === "home" ? "Home" : "Away"), rr, acting ? h("span", "st-turn-tag", "to play") : null));
    }

    function render(game) {
      lastGame = game;
      const st = game.state, home = st.home_team, away = st.away_team;
      const board = game.arena.board, W = board[0].length - 2, H = board.length - 2;
      const names = opts.names ? opts.names() : {};
      // header: away (red, left) - score - home (blue, right), matching the pitch orientation
      head.innerHTML = "";
      const turn = Math.max(home.state.turn || 0, away.state.turn || 0);
      const phase = st.game_over ? "Full time" : (turn ? `Half ${st.half} · Turn ${turn}` : `Half ${st.half || 1} · Kick-off`);
      const w = WEATHER[st.weather] || "nice";
      const centre = h("div", "st-centre",
        h("div", "st-score", h("span", "away", String(away.state.score)), h("span", "dash", "–"), h("span", "home", String(home.state.score))),
        h("div", "st-phase", phase, " · "),
      );
      const wimg = document.createElement("img");
      wimg.src = `${IMG}/weather/${String(st.weather).toLowerCase()}.gif`; wimg.alt = pretty(st.weather); wimg.title = `Weather: ${pretty(st.weather)}`;
      centre.lastChild.append(wimg);
      if (opts.badge) centre.prepend(opts.badge());
      head.append(teamHead(away, "away", game, names.away), centre, teamHead(home, "home", game, names.home));

      // pitch
      pitch.style.aspectRatio = `${W} / ${H}`;
      pitch.style.backgroundImage = `url(${IMG}/arenas/pitch/${w}-${W}x${H}.jpg)`;
      pitch.style.setProperty("--ar", (W / H).toFixed(4));
      layer.innerHTML = "";
      const at = (x, y) => ({ left: `${((x - 1) / W) * 100}%`, top: `${((y - 1) / H) * 100}%`, width: `${100 / W}%`, height: `${100 / H}%` });
      const place = (el, x, y) => { Object.assign(el.style, at(x, y)); return el; };
      // the active player's path this action
      const activeId = st.active_player_id;
      const activeTeam = activeId && home.players_by_id[activeId] ? "home" : "away";
      (game.squares_moved || []).forEach((sq) => layer.append(place(h("div", `st-step ${activeTeam}`), sq.x, sq.y)));
      const balls = (st.pitch && st.pitch.balls) || st.balls || [];
      const carrierAt = new Set(balls.filter((b) => b.is_carried && b.position).map((b) => `${b.position.x},${b.position.y}`));
      for (const [side, team] of [["home", home], ["away", away]]) {
        for (const p of Object.values(team.players_by_id)) {
          if (!p.position) continue;
          const s = p.state || {};
          const active = p.player_id === activeId;
          const cls = ["st-player", side, s.up ? "" : "down", s.stunned ? "stunned" : "", s.used ? "used" : "", active ? "active" : ""].join(" ");
          const el = place(h("div", cls), p.position.x, p.position.y);
          el.dataset.pid = p.player_id;
          const img = document.createElement("img");
          img.src = sprite(team.race, p.role, side === "home", active); img.alt = "";
          el.append(img, h("span", "st-nr", String(p.nr)));
          if (carrierAt.has(`${p.position.x},${p.position.y}`)) el.append(h("span", "st-ball carried"));
          if (s.stunned) el.append(h("span", "st-status", "z"));
          el.setAttribute("aria-label", `${side === "home" ? "Home" : "Away"} #${p.nr} ${p.name}, ${p.role}`);
          layer.append(el);
        }
      }
      balls.filter((b) => b.position && !b.is_carried).forEach((b) => layer.append(place(h("div", "st-ballsq", h("span", `st-ball ${b.on_ground ? "" : "air"}`)), b.position.x, b.position.y)));

      // dugouts
      dugouts.innerHTML = "";
      for (const [side, team, dug] of [["away", away, st.away_dugout], ["home", home, st.home_dugout]]) {
        const box = h("div", `st-dugout ${side}`);
        const group = (label, ids) => {
          if (!ids || !ids.length) return null;
          const g = h("span", "st-dg", h("span", "st-dg-label", label));
          ids.forEach((id) => {
            const p = team.players_by_id[id]; if (!p) return;
            const img = document.createElement("img"); img.src = sprite(team.race, p.role, side === "home", false);
            img.alt = `#${p.nr} ${p.name} (${p.role})`; img.dataset.pid = p.player_id; g.append(img);
          });
          return g;
        };
        const parts = [group("Reserves", dug && dug.reserves), group("KO", dug && dug.kod), group("Cas", dug && dug.casualties)].filter(Boolean);
        box.append(...(parts.length ? parts : [h("span", "st-dg-label", "Dugout empty")]));
        dugouts.append(box);
      }

      // who is acting right now, always visible (no hover needed)
      now.innerHTML = "";
      const act = !st.game_over && activeId && findPlayer(game, activeId);
      now.hidden = !act;
      if (act) {
        const img = document.createElement("img");
        img.src = sprite(act.team.race, act.p.role, act.side === "home", false); img.alt = "";
        const bits = doing(game, act.side, act.p).filter((t) => t !== "Already acted this turn");
        const moved = (act.p.state && act.p.state.moves) || 0;
        if (moved || ["MOVE", "BLITZ", "PASS", "HANDOFF", "FOUL"].includes(st.player_action_type)) {
          const left = act.p.ma - moved;
          bits.push(left >= 0 ? `${left} MA left` : `${-left} GFI`);
        }
        now.dataset.pid = activeId;
        now.className = `st-now ${act.side}`;
        now.append(h("span", "st-now-label", "Now"), img,
          h("b", null, `${act.side === "home" ? "Home" : "Away"} #${act.p.nr} ${act.p.role}`),
          bits.length ? h("span", null, " · " + bits.join(" · ")) : null);
      }

      // the last few game events (server-side text, perspective-neutral)
      const log = (game.bench && game.bench.log) || [];
      ticker.innerHTML = "";
      ticker.hidden = !log.length;
      [...log].reverse().slice(0, 4).forEach((t, i) => ticker.append(h("li", i ? "" : "latest", t)));

      showCard();
    }

    // live mode: poll the match's latest state (the server answers 304 when nothing changed)
    let liveTimer = null;
    function live(matchId, onState) {
      stop();
      const tick = () => fetch(`/api/matches/${matchId}/state`, { cache: "no-cache" }).then((r) => {
        if (!r.ok) throw new Error(r.status);
        const tag = r.headers.get("ETag");
        if (tag && tag === lastKey) return null;
        lastKey = tag;
        return r.json();
      }).then((g) => {
        if (g) { render(g); onState && onState(g); }
        if (!(g && g.state.game_over)) liveTimer = setTimeout(tick, 800);
      }).catch(() => { liveTimer = setTimeout(tick, 3000); });
      tick();
    }
    function stop() { clearTimeout(liveTimer); }

    // replay mode: fetch recorded frames (immutable, cached by the browser) with a small prefetch window
    const cache = new Map();
    let wanted = null;
    const fetchFrame = (matchId, i) => {
      if (!cache.has(i)) cache.set(i, fetch(`/api/matches/${matchId}/frames/${i}`).then((r) => r.json()));
      return cache.get(i);
    };
    function frame(matchId, i, total) {
      wanted = i;
      fetchFrame(matchId, i).then((g) => { if (wanted === i) render(g); });
      for (let k = 1; k <= 4; k++) if (total === undefined || i + k < total) fetchFrame(matchId, i + k);
      for (const key of cache.keys()) if (Math.abs(key - i) > 40) cache.delete(key);
    }

    return { render, live, stop, frame };
  }

  return { create };
})();
