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
    const ticker = h("ol", "st-ticker");
    ticker.setAttribute("aria-label", "Latest game events");
    root.append(head, pitchWrap, dugouts, ticker);
    let lastKey = null;

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
          const img = document.createElement("img");
          img.src = sprite(team.race, p.role, side === "home", active); img.alt = "";
          el.append(img, h("span", "st-nr", String(p.nr)));
          if (carrierAt.has(`${p.position.x},${p.position.y}`)) el.append(h("span", "st-ball carried"));
          if (s.stunned) el.append(h("span", "st-status", "z"));
          const skills = [...(p.role_skills || []), ...(p.extra_skills || [])].map(pretty).join(", ");
          el.title = `${side === "home" ? "H" : "A"}${p.nr} · ${p.name} (${p.role})\nMA${p.ma} ST${p.st} AG${p.ag}+ AV${p.av}+${skills ? "\n" + skills : ""}\n${s.stunned ? "Stunned" : s.up ? "Standing" : "Prone"}${s.used ? " · used this turn" : ""}`;
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
            img.alt = `#${p.nr}`; img.title = `#${p.nr} ${p.name} (${p.role})`; g.append(img);
          });
          return g;
        };
        const parts = [group("Reserves", dug && dug.reserves), group("KO", dug && dug.kod), group("Cas", dug && dug.casualties)].filter(Boolean);
        box.append(...(parts.length ? parts : [h("span", "st-dg-label", "Dugout empty")]));
        dugouts.append(box);
      }

      // the last few game events (server-side text, perspective-neutral)
      const log = (game.bench && game.bench.log) || [];
      ticker.innerHTML = "";
      ticker.hidden = !log.length;
      [...log].reverse().slice(0, 4).forEach((t, i) => ticker.append(h("li", i ? "" : "latest", t)));
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
