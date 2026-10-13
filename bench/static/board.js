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

  // ---- reading play out of consecutive frames ----------------------------------------------------------
  // The server records one frame per decision. A "step" is the change from one frame to the next; an
  // "activation" is the run of frames while one player acts. Players are keyed by the short ids the server's
  // log uses (H5 = home #5, A1 = away #1), so log lines can be matched to the board.
  const SVGNS = "http://www.w3.org/2000/svg";
  const VERB = { MOVE: "move", BLOCK: "block", BLITZ: "blitz", PASS: "pass", HANDOFF: "hand-off", FOUL: "foul" };
  const sameSq = (a, b) => a && b && a[0] === b[0] && a[1] === b[1];
  const dieName = (d) => d.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());

  function slim(game, prev) {
    const st = game.state, p = {}, ids = {}, info = {};
    for (const [s, team] of [["H", st.home_team], ["A", st.away_team]]) {
      for (const pl of Object.values(team.players_by_id)) {
        ids[pl.player_id] = s + pl.nr;
        if (!pl.position) continue;
        p[s + pl.nr] = [pl.position.x, pl.position.y];
        const ps = pl.state || {}, sk = [...(pl.role_skills || []), ...(pl.extra_skills || [])];
        // botbowl's Player.has_tackle_zone: standing, not distracted, not Titchy
        info[s + pl.nr] = { ag: pl.ag, ma: pl.ma, mv: ps.moves || 0, sk,
          tz: !!ps.up && !ps.bone_headed && !ps.hypnotized && !ps.really_stupid && !sk.includes("TITCHY") };
      }
    }
    const balls = (st.pitch && st.pitch.balls) || st.balls || [];
    const b0 = balls.find((b) => b.position);
    const full = (game.bench && game.bench.log) || [];
    let log;
    if (prev) {
      // the log is a sliding window of the last few lines: keep only what's new since the previous frame
      let k = 0;
      for (let j = Math.min(prev.full.length, full.length); j > 0; j--) {
        if (prev.full.slice(-j).every((t, n) => t === full[n])) { k = j; break; }
      }
      log = full.slice(k);
    } else {
      // no previous frame (a jump in a replay): take the lines of the current activation
      let s = -1;
      full.forEach((t, n) => { if (/ starts a \w+ action\./.test(t)) s = n; });
      log = s >= 0 ? full.slice(s) : full.slice(-2);
    }
    return {
      half: st.half, turns: [st.home_team.state.turn, st.away_team.state.turn],
      score: [st.home_team.state.score, st.away_team.state.score],
      cur: st.current_team_id === st.home_team.team_id ? "H" : st.current_team_id === st.away_team.team_id ? "A" : null,
      act: ids[st.active_player_id] || null, at: st.player_action_type,
      sm: (game.squares_moved || []).map((q) => [q.x, q.y]),
      ball: b0 ? [b0.position.x, b0.position.y, b0.is_carried ? "c" : b0.on_ground ? "g" : "a"] : null,
      p, info, log, full, over: !!st.game_over, blizzard: st.weather === "BLIZZARD",
    };
  }
  const turnKey = (f) => `${f.half}-${f.cur}-${f.cur === "H" ? f.turns[0] : f.turns[1]}`;
  const scored = (a, b) => a && b && (a.score[0] !== b.score[0] || a.score[1] !== b.score[1]);
  function carrierOf(f) {
    if (!f.ball || f.ball[2] !== "c") return null;
    for (const id in f.p) if (sameSq(f.p[id], f.ball)) return id;
    return null;
  }
  // how each player got from frame a to frame b: kind is walk (along their path), push, or place (arrived)
  function stepMoves(a, b) {
    const out = [];
    for (const id in b.p) {
      const p0 = a.p[id], p1 = b.p[id];
      if (!p0) { out.push({ id, pts: [p1], kind: "place" }); continue; }
      if (sameSq(p0, p1)) continue;
      let pts = null, kind = "push";
      if (id === b.act && b.sm.length) {
        const k = b.sm.findIndex((q) => sameSq(q, p0));
        if (k >= 0 && sameSq(b.sm[b.sm.length - 1], p1)) { pts = b.sm.slice(k); kind = "walk"; }
        else if (Math.max(Math.abs(p1[0] - p0[0]), Math.abs(p1[1] - p0[1])) > 1 || b.at === "MOVE") kind = "walk";
      }
      if (kind === "push" && (id === b.act || id === a.act) && b.log.some((t) => t.startsWith(id + " follows up"))) kind = "walk";
      out.push({ id, pts: pts || [p0, p1], kind });
    }
    return out;
  }
  function stepBall(a, b) {
    if (!a.ball || !b.ball || sameSq(a.ball, b.ball)) return null;
    const c0 = carrierOf(a), c1 = carrierOf(b);
    if (c0 && c0 === c1) return { kind: "carried", by: c1, from: a.ball, to: b.ball };
    const thrown = a.at === "PASS" || a.at === "HANDOFF" || b.log.some((t) => /pass|throw|hand-?off|catch|caught|intercept/i.test(t));
    return { kind: thrown ? "flight" : "loose", from: a.ball, to: b.ball };
  }
  // the frames [s..e] of the latest activation; its trails stay up until the next player is activated
  function activation(hist) {
    const e = hist.length - 1;
    let j = e;
    while (j >= 0 && !hist[j].act) { if (turnKey(hist[j]) !== turnKey(hist[e])) return null; j--; }
    if (j < 0 || turnKey(hist[j]) !== turnKey(hist[e])) return null;
    const act = hist[j].act;
    let s = j;
    while (s > 0 && hist[s - 1].act === act && turnKey(hist[s - 1]) === turnKey(hist[j])) s--;
    return { act, s, e };
  }
  function routes(hist, s, e) {
    const by = {};
    for (let k = Math.max(1, s); k <= e; k++) {
      if (scored(hist[k - 1], hist[k])) return {};
      for (const m of stepMoves(hist[k - 1], hist[k])) {
        // the step into the activation's first frame can carry the end of the previous one (live polls skip frames)
        if (m.kind === "place" || (k === s && m.id !== hist[s].act)) continue;
        (by[m.id] = by[m.id] || []).push(m);
      }
    }
    // the acting player's route this action, even when earlier frames of it weren't seen
    const act = hist[e].act || hist[s].act;
    if (act && !by[act]) {
      for (let k = e; k >= s; k--) {
        if (hist[k].act === act && hist[k].sm.length > 1) { by[act] = [{ id: act, pts: hist[k].sm, kind: "walk" }]; break; }
      }
    }
    return by;
  }
  function ballSteps(hist, s, e) {
    const out = [];
    for (let k = Math.max(1, s + 1); k <= e; k++) {
      if (scored(hist[k - 1], hist[k])) return [];
      const b = stepBall(hist[k - 1], hist[k]);
      if (b && b.kind !== "carried") out.push(b);
    }
    return out;
  }
  // the latest block in frames [s..e]: who hit whom, and the die that was used
  function lastBlock(hist, s, e) {
    let blk = null;
    for (let k = s; k <= e; k++) for (const t of hist[k].log) {
      if (/ starts an? \w+ action\./.test(t)) blk = null;   // a block before this activation isn't part of it
      const m = t.match(/^([HA]\d+) blocks ([HA]\d+)\.(?: \[roll ([^\]]+)\])?/);
      if (m) blk = { a: m[1], t: m[2], dice: m[3] || "", pick: null, at: null };
      const d = t.match(/selects block die\. \[roll ([^\]]+)\]/);
      if (d && blk) blk.pick = d[1];
    }
    if (blk && !blk.pick && blk.dice && !blk.dice.includes("/")) blk.pick = blk.dice;
    if (blk) for (let k = s; k <= e && !blk.at; k++) blk.at = hist[k].p[blk.t];   // where the target stood when hit
    return blk;
  }

  // ---- tackle zones and the rolls a move needed ------------------------------------------------------
  // A standing player "marks" the 8 squares around them. Leaving a marked square needs a dodge roll; every
  // marking player around the square being entered makes it harder. Rules as botbowl applies them (BB2016).
  const sqKey = (q) => `${q[0]},${q[1]}`;
  function zones(f, side, W, H) {
    const z = new Map();
    for (const id in f.p) {
      if (id[0] !== side || !(f.info[id] && f.info[id].tz)) continue;
      const [x, y] = f.p[id];
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        if (!dx && !dy) continue;
        const q = [x + dx, y + dy];
        if (q[0] < 1 || q[1] < 1 || q[0] > W || q[1] > H) continue;
        const k = sqKey(q), e = z.get(k) || { n: 0, by: [] };
        e.n++; e.by.push(id); z.set(k, e);
      }
    }
    return z;
  }
  const clampRoll = (n) => Math.max(2, Math.min(6, n));
  function dodgeNeed(pi, tzTo) {
    let mod = 1;
    const stunty = pi.sk.includes("STUNTY") || pi.sk.includes("TITCHY");
    if (pi.sk.includes("TITCHY")) mod++;
    if (pi.sk.includes("TWO_HEADS")) mod++;
    if (!stunty) mod -= tzTo;
    return clampRoll(7 - pi.ag - mod);
  }
  // the dice in the activation's log lines, grouped into one entry per roll attempt (a failure and its re-roll together)
  function rollGroups(act, lines) {
    const out = [];
    let cur = null, rr = null;
    for (const t of lines) {
      if (/uses a team re-roll/.test(t)) { rr = "team re-roll"; if (cur && !cur.done) cur.rr = rr; continue; }
      const sk = t.match(new RegExp(`^${act} uses the (.+) skill\\.`));
      if (sk) { rr = `${sk[1]} skill`; if (cur && !cur.done) cur.rr = rr; else { cur = { kind: /sure feet/i.test(sk[1]) ? "gfi" : "dodge", rolls: [{ r: "", ok: false }], rr, done: false, cut: true }; out.push(cur); } continue; }
      let m = t.match(new RegExp(`^${act} (successfully dodged|failed to dodge)\\. \\[roll (\\d)\\]`)), kind = "dodge";
      if (!m) { m = t.match(new RegExp(`^${act} GFI (was successful|failed)\\. \\[roll (\\d)\\]`)); kind = "gfi"; }
      if (m) {
        const ok = /success/.test(m[1]);
        if (!cur || cur.done || cur.kind !== kind) { cur = { kind, rolls: [], rr: null, done: false }; out.push(cur); }
        cur.rolls.push({ r: m[2], ok, rr: cur.rolls.length ? rr : null });
        cur.done = ok; rr = null;
        continue;
      }
      if (cur && t.startsWith(act) && /goes to the ground|falls|knocked down/.test(t)) { cur.fell = true; cur.done = true; }
    }
    return out;
  }
  // every square the acting player stepped through this activation, with the dodges and GFIs it took
  function runSteps(hist, A, W, H) {
    const act = A.act, opp = act[0] === "H" ? "A" : "H", steps = [];
    let lines = [];
    for (let k = A.s; k <= A.e; k++) lines.push(...hist[k].log);
    const st = lines.reduce((j, t, n) => (t.startsWith(`${act} starts a`) ? n : j), -1);
    const cut = st < 0;   // the server sends only the last few log lines, so a long run can lose its first rolls
    lines = cut ? lines : lines.slice(st);
    let moves = null;
    for (let k = Math.max(1, A.s); k <= A.e; k++) {
      const a = hist[k - 1], b = hist[k];
      if (!a.info[act]) continue;
      const m = stepMoves(a, b).find((x) => x.id === act && x.kind === "walk");
      let pts = m ? m.pts : [];
      // a roll that failed and waits on a re-roll: the path already names the square being tried
      const p1 = b.p[act], j = b.act === act && p1 ? b.sm.findIndex((q) => sameSq(q, p1)) : -1;
      const pend = j >= 0 && j < b.sm.length - 1 ? b.sm.slice(j) : [];
      if (!pts.length && !pend.length) continue;
      const z = zones(a, opp, W, H), pi = a.info[act];
      if (moves === null) moves = pi.mv;
      const add = (from, to, pending) => {
        const last = steps[steps.length - 1];
        if (last && last.pending && sameSq(last.from, from) && sameSq(last.to, to)) { last.pending = pending; return; }
        moves++;
        const zf = z.get(sqKey(from)), zt = z.get(sqKey(to));
        const dodge = !!zf;
        steps.push({ from, to, dodge, pending, gfi: moves > pi.ma, markers: zf ? zf.by : [], tzTo: zt ? zt.n : 0,
          need: dodge ? dodgeNeed(pi, zt ? zt.n : 0) : null, gfiNeed: b.blizzard ? 3 : 2 });
      };
      for (let i = 0; i + 1 < pts.length; i++) add(pts[i], pts[i + 1], false);
      for (let i = 0; i + 1 < pend.length; i++) add(pend[i], pend[i + 1], true);
    }
    // attach the dice: in order when the whole activation is in the log, otherwise from the end
    const groups = rollGroups(act, lines);
    for (const kind of ["dodge", "gfi"]) {
      const gs = groups.filter((g) => g.kind === kind), ss = steps.filter((s) => s[kind]);
      if (cut) { const off = ss.length - gs.length; gs.forEach((g, i) => { if (ss[off + i]) ss[off + i][kind + "Roll"] = g; }); }
      else gs.forEach((g, i) => { if (ss[i]) ss[i][kind + "Roll"] = g; });
    }
    steps.forEach((s, i) => {
      for (const kind of ["dodge", "gfi"]) if (s[kind] && !s[kind + "Roll"] && i < steps.length - 1) s[kind + "Roll"] = { rolls: [{ r: "", ok: true }], inferred: true };
    });
    return { act, steps };
  }
  // the latest activation, kept on screen after a turnover until somebody else is activated
  function lastActivation(hist) {
    const A = activation(hist);
    if (A) return A;
    let j = hist.length - 1;
    while (j >= 0 && !hist[j].act) j--;
    if (j < 0) return null;
    const act = hist[j].act;
    let s = j;
    while (s > 0 && hist[s - 1].act === act) s--;
    return { act, s, e: hist.length - 1, after: true };
  }

  function create(root, opts = {}) {
    root.classList.add("stadium");
    root.innerHTML = "";
    const head = h("div", "st-head");
    const possess = h("div", "st-possess");
    const pitchWrap = h("div", "st-pitch-wrap");
    const pitch = h("div", "st-pitch");
    const layer = h("div", "st-layer");
    const banner = h("div", "st-banner");
    banner.setAttribute("aria-live", "polite");
    pitch.append(layer, banner);
    pitchWrap.append(pitch);
    const caption = h("div", "st-caption");
    const rolls = h("div", "st-rolls");
    let dims = null, lastRun = null, shownZside = null;
    const hz = document.createElementNS(SVGNS, "svg");
    hz.setAttribute("class", "st-svg hz");
    caption.setAttribute("aria-live", "polite");
    const dugouts = h("div", "st-dugouts");
    const now = h("div", "st-now");
    const ticker = h("ol", "st-ticker");
    ticker.setAttribute("aria-label", "Latest game events");
    if (opts.onLog) {
      ticker.classList.add("clickable");
      ticker.title = "Open the full game log";
      ticker.addEventListener("click", () => opts.onLog());
    }
    const card = h("div", "st-card");
    card.hidden = true;
    root.append(head, possess, pitchWrap, caption, rolls, now, dugouts, ticker, card);
    let lastKey = null;
    let lastGame = null;
    let hist = [];          // recent frames in order, for movement and trails
    let anims = [];
    let bannerTimer = null;
    const reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

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
    // short id (H5) -> player, in the latest frame
    function byShort(sid) {
      if (!lastGame || !sid) return null;
      const team = sid[0] === "H" ? lastGame.state.home_team : lastGame.state.away_team;
      return Object.values(team.players_by_id).find((p) => String(p.nr) === sid.slice(1)) || null;
    }
    const sideName = (sid) => (sid[0] === "H" ? "Home" : "Away");
    const shortName = (sid) => `${sideName(sid)} #${sid.slice(1)}`;
    const longName = (sid) => { const p = byShort(sid); return p ? `${shortName(sid)} ${p.role}` : shortName(sid); };

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

    function drawHoverZone(id) {
      hz.innerHTML = "";
      const sid = id && lastGame && hist.length ? (() => { const f = findPlayer(lastGame, id); return f ? (f.side === "home" ? "H" : "A") + f.p.nr : null; })() : null;
      const f = hist[hist.length - 1];
      if (!sid || !dims || !f || !f.p[sid] || !(f.info[sid] && f.info[sid].tz)) return;
      hz.setAttribute("viewBox", `0 0 ${dims.W} ${dims.H}`); hz.setAttribute("preserveAspectRatio", "none");
      const [x, y] = f.p[sid];
      svg("rect", { x: x - 2 + 0.06, y: y - 2 + 0.06, width: 2.88, height: 2.88, rx: 0.18, class: "tz-hover", style: `stroke:${teamVar(sid)}` }, hz);
      svg("text", { x: x - 0.5, y: y + 1.12 > dims.H ? y - 2.2 : y + 1.25, class: "tz-hover-tx" }, hz).textContent = "TACKLE ZONE";
    }
    function showCard() {
      const id = hoverId || pinnedId;
      drawHoverZone(id);
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

    // ---- overlays: routes, pushes, blocks, the ball's flight (SVG in square units, 1 unit = 1 square) ----
    const svg = (tag, attrs, parent) => {
      const e = document.createElementNS(SVGNS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.append(e);
      return e;
    };
    const cx = (x) => x - 0.5, cy = (y) => y - 0.5;
    const pathD = (pts) => pts.map((q, k) => `${k ? "L" : "M"}${cx(q[0])} ${cy(q[1])}`).join(" ");
    const arcD = (a, b, lift) => `M${cx(a[0])} ${cy(a[1])} Q${(cx(a[0]) + cx(b[0])) / 2} ${(cy(a[1]) + cy(b[1])) / 2 - lift} ${cx(b[0])} ${cy(b[1])}`;
    const teamVar = (sid) => (sid[0] === "H" ? "var(--home)" : "var(--away)");
    function svgLayer(W, H, cls) {
      const s = svg("svg", { class: `st-svg ${cls}`, viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
      const defs = svg("defs", {}, s);
      for (const [name, col] of [["H", "var(--home)"], ["A", "var(--away)"], ["ball", "var(--ball)"], ["push", "#fff"]]) {
        const m = svg("marker", { id: `st-ar-${cls}-${name}`, viewBox: "0 0 10 10", refX: 7, refY: 5, markerWidth: 3.2, markerHeight: 3.2,
          orient: "auto-start-reverse", markerUnits: "strokeWidth" }, defs);
        svg("path", { d: "M0 0 L10 5 L0 10 z", style: `fill:${col}` }, m);
      }
      return s;
    }
    const marker = (cls, name) => `url(#st-ar-${cls}-${name})`;
    function chip(parent, x, y, text) {
      const w = 0.17 * text.length + 0.3;
      svg("rect", { x: x - w / 2, y: y - 0.21, width: w, height: 0.42, rx: 0.05, class: "st-chip-bg" }, parent);
      svg("text", { x, y: y + 0.01, class: "st-chip-tx" }, parent).textContent = text;
    }

    function rollText(g, need) {
      if (!g) return need ? `${need}+` : "";
      const rs = g.rolls.map((r, i) => `${i ? "↻ " : ""}${r.r}${r.ok ? "✓" : "✗"}`).join(" ");
      return need ? `${need}+: ${rs}` : rs;
    }
    function badge(parent, x, y, text, cls, step) {
      const w = 0.155 * [...text].length + 0.26;
      if (step) parent = svg("g", { class: "tz-b", "data-step": `${sqKey(step.from)}>${sqKey(step.to)}` }, parent);
      svg("rect", { x: x - w / 2, y: y - 0.2, width: w, height: 0.4, rx: 0.08, class: `tz-badge ${cls}` }, parent);
      svg("text", { x, y: y + 0.015, class: `tz-badge-tx ${cls}` }, parent).textContent = text;
    }
    function drawZones(under, f, W, H, A, run) {
      const def = f.cur === "H" ? "A" : f.cur === "A" ? "H" : null;
      const runSide = run && run.act[0];
      const zside = run && A && A.after ? (runSide === "H" ? "A" : "H") : def;
      shownZside = zside && f.cur ? zside : null;
      if (!zside || !f.cur) return;
      // zones as they stand now: a player knocked down, sent off or scored on during this activation loses theirs
      // (each dodge badge was already worked out from the zones at the moment of that step)
      const z = zones(f, zside, W, H);
      const col = zside === "H" ? "var(--home)" : "var(--away)";
      const g = svg("g", { class: "tz" }, under);
      for (const [k, e] of z) {
        const [x, y] = k.split(",").map(Number);
        svg("rect", { x: x - 1 + 0.04, y: y - 1 + 0.04, width: 0.92, height: 0.92, class: `tz-sq n${Math.min(e.n, 3)}`, style: `fill:${col};stroke:${col}` }, g);
        if (e.n > 1) svg("text", { x: x - 0.9, y: y - 0.08, class: "tz-n" }, g).textContent = `×${e.n}`;
      }
    }
    function drawRun(under, over, f, H, A, run) {
      if (!run || !run.steps.length) return;
      const colA = teamVar(run.act);
      const own = A && !A.after ? routes(hist, A.s, A.e)[run.act] : null;
      if (!own) {   // the board drops trails on a touchdown or turnover; keep this move's route up
        const pts = [run.steps[0].from, ...run.steps.map((s) => s.to)];
        svg("path", { d: pathD(pts), class: "st-route-shadow" }, under);
        svg("path", { d: pathD(pts), class: "st-route", style: `stroke:${colA}` }, under);
      }
      for (const s of run.steps) {
        if (s.dodge) {
          // the dodge step: a bright dashed jump out of the zone
          const seg = { x1: cx(s.from[0]), y1: cy(s.from[1]), x2: cx(s.to[0]), y2: cy(s.to[1]) };
          svg("line", { ...seg, class: "tz-jump" }, under);
        }
      }
      for (const s of run.steps) {
        const ok = (g) => !g || g.rolls[g.rolls.length - 1].ok;
        const lift = s.to[1] <= 1 ? 0.78 : -0.72;
        if (s.dodge) {
          const g = s.dodgeRoll;
          badge(over, cx(s.to[0]), cy(s.to[1]) + lift, rollText(g, s.need) + (g && !ok(g) && s.pending && !g.fell ? " ↻?" : ""), g ? (ok(g) ? "ok" : "bad") : "", s);
          if (s.pending) svg("rect", { x: s.to[0] - 1 + 0.06, y: s.to[1] - 1 + 0.06, width: 0.88, height: 0.88, rx: 0.1, class: "tz-try" }, under);
        } else if (s.gfi) {
          const g = s.gfiRoll;
          badge(over, cx(s.to[0]), cy(s.to[1]) + lift, `GFI ${rollText(g, s.gfiNeed)}`, `gfi ${g ? (ok(g) ? "ok" : "bad") : ""}`, s);
        }
        if ((s.dodgeRoll && s.dodgeRoll.fell) || (s.gfiRoll && s.gfiRoll.fell)) {
          const x = cx(s.to[0]), y = cy(s.to[1]);
          svg("path", { "data-step": `${sqKey(s.from)}>${sqKey(s.to)}`, d: `M${x - 0.32} ${y - 0.32}L${x + 0.32} ${y + 0.32}M${x + 0.32} ${y - 0.32}L${x - 0.32} ${y + 0.32}`, class: "tz-fell" }, over);
        }
      }
    }

    function drawOverlays(under, over, f, H, W) {
      const carrier = carrierOf(f);
      const RA = lastActivation(hist);
      const run = lastRun = RA ? runSteps(hist, RA, W, H) : null;
      drawZones(under, f, W, H, RA, run);
      if (f.ball) {
        const [bx, by] = f.ball;
        if (carrier) {
          svg("circle", { cx: cx(bx), cy: cy(by), r: 0.47, class: "st-halo" }, under);
        } else {
          svg("circle", { cx: cx(bx), cy: cy(by), r: 0.42, class: "st-loose" }, under);
          svg("text", { x: cx(bx), y: by >= H ? cy(by) - 0.62 : cy(by) + 0.7, class: "st-loose-tx" }, over).textContent = "LOOSE BALL";
        }
      }
      const n = hist.length - 1;
      // the ball's latest flight or bounce stays drawn even across a turnover
      const sb = n > 0 ? stepBall(hist[n - 1], hist[n]) : null;
      const A = activation(hist);
      if (sb && sb.kind !== "carried" && !(A && A.e === n && n >= Math.max(1, A.s))) {
        svg("path", { d: arcD(sb.from, sb.to, sb.kind === "flight" ? 1.2 : 0.4), class: "st-ballpath", "marker-end": marker("over", "ball") }, over);
      }
      drawRun(under, over, f, H, RA, run);
      if (!A) return;
      const rs = routes(hist, A.s, A.e);
      for (const id in rs) for (const m of rs[id]) {
        if (m.kind === "walk") {
          svg("path", { d: pathD(m.pts), class: "st-route-shadow" }, under);
          svg("path", { d: pathD(m.pts), class: "st-route", style: `stroke:${teamVar(id)}`, "marker-end": marker("under", id[0]) }, under);
          m.pts.slice(1, -1).forEach((q) => svg("circle", { cx: cx(q[0]), cy: cy(q[1]), r: 0.08, style: `fill:${teamVar(id)}` }, under));
          if (id === A.act) svg("circle", { cx: cx(m.pts[0][0]), cy: cy(m.pts[0][1]), r: 0.2, class: "st-route-start", style: `stroke:${teamVar(id)}` }, under);
        } else {
          svg("path", { d: pathD(m.pts), class: "st-push", "marker-end": marker("under", id[0]) }, under);
        }
      }
      for (const b of ballSteps(hist, A.s, A.e)) {
        svg("path", { d: arcD(b.from, b.to, b.kind === "flight" ? 1.2 : 0.4), class: "st-ballpath", "marker-end": marker("over", "ball") }, over);
      }
      const blk = lastBlock(hist, A.s, A.e);
      const pa = blk && f.p[blk.a], pt = blk && (blk.at || f.p[blk.t]);
      if (pa && pt) {
        const ax = cx(pa[0]), ay = cy(pa[1]), tx = cx(pt[0]), ty = cy(pt[1]);
        const dx = tx - ax, dy = ty - ay, L = Math.hypot(dx, dy);
        if (L > 0.3) {
          const seg = { x1: ax + dx / L * 0.32, y1: ay + dy / L * 0.32, x2: tx - dx / L * 0.3, y2: ty - dy / L * 0.3 };
          svg("line", { ...seg, class: "st-block-shadow" }, over);
          svg("line", { ...seg, class: "st-block", style: `stroke:${teamVar(blk.a)}`, "marker-end": marker("over", blk.a[0]) }, over);
        }
        svg("circle", { cx: tx, cy: ty, r: 0.48, class: "st-hit", style: `stroke:${teamVar(blk.a)}` }, under);
        const res = blk.pick || blk.dice;
        if (res) chip(over, tx, ty >= H ? ty - 0.75 : ty + 0.72, res.split("/").map(dieName).join(" / "));
      }
    }

    // ---- plain-English captions and the possession line ----
    function humanize(t) {
      let roll = "";
      t = t.replace(/\s*\[roll ([^\]]+)\]/, (_, r) => { roll = r; return ""; });
      t = t.replace(/Home\(H\)/g, "Home").replace(/Away\(A\)/g, "Away");
      const seen = new Set();
      t = t.replace(/\b([HA]\d+)\b/g, (m) => { const s = seen.has(m) ? shortName(m) : longName(m); seen.add(m); return s; });
      t = t.replace(/ starts an? (\w+) action\./, (_, a) => ` starts a ${a}.`)
           .replace(/selects block die\./, "picks the block die.")
           .replace(/ Turn (\d+)\./, " turn $1 begins.");
      return { text: t, roll: roll ? roll.split("/").map(dieName).join(" / ") : "" };
    }
    const RANK = (t) => /scored|turnover|intercept|picked up|caught|failed|fumble|KO|injur|casualt|stunned|knocked/i.test(t) ? 3
      : /blocks|pushed|dodge|follows|bounced|pass|hand/i.test(t) ? 2 : /turn \d+ begins|starts an? /.test(t) ? 0 : 1;
    function renderCaption() {
      // the latest frame that added something to the log; frames that only wait for a decision keep the caption
      let n = hist.length - 1;
      while (n > 0 && !hist[n].log.length) n--;
      const f = hist[n];
      const lines = f.log.map(humanize);
      let main = null, sub = [];
      const run = n > 0 ? stepMoves(hist[n - 1], f).find((m) => m.kind === "walk" && m.pts.length > 2) : null;
      if (run) {
        const sq = run.pts.length - 1;
        let dodges = f.log.filter((t) => /dodge/.test(t)).length, gfis = 0;
        if (lastRun && lastRun.act === run.id) { dodges = lastRun.steps.filter((s) => s.dodge).length; gfis = lastRun.steps.filter((s) => s.gfi).length; }
        const times = (n) => (n === 1 ? "once" : n === 2 ? "twice" : `${n} times`);
        const bits = [dodges ? `dodging ${times(dodges)}` : null, gfis ? `going for it ${times(gfis)}` : null].filter(Boolean);
        main = { text: `${longName(run.id)} runs ${sq} squares${bits.length ? ", " + bits.join(" and ") : ""}`, roll: "" };
        sub = lines.slice(-2);
      } else if (lines.length) {
        let best = lines.length - 1;
        lines.forEach((l, k) => { if (RANK(l.text) >= RANK(lines[best].text)) best = k; });
        main = lines[best];
        sub = lines.filter((_, k) => k !== best).slice(-2);
      }
      const act = f.act || (activation(hist) || {}).act;
      const side = (act && act[0]) || f.cur;
      caption.className = `st-caption ${side === "H" ? "home" : side === "A" ? "away" : ""}`;
      caption.innerHTML = "";
      // always shown at a fixed height (long lines are clipped, the full text is in the tooltip) so the board
      // and the replay controls below it don't jump between frames
      caption.title = main ? [main, ...sub].map((l) => l.text + (l.roll ? ` [${l.roll}]` : "")).join("\n") : "";
      if (!main) { caption.className = "st-caption"; return; }
      const piece = (l) => [l.text, l.roll ? h("span", "st-dice", l.roll) : null];
      caption.append(h("span", "st-cap-who", side ? (side === "H" ? "Home" : "Away") : "Game"),
        h("span", "st-cap-what", h("span", "st-cap-main", ...piece(main)),
          sub.length ? h("small", null, ...sub.flatMap((l, k) => [k ? " · " : null, ...piece(l)])) : null));
    }
    function renderRolls() {
      rolls.innerHTML = "";
      const run = lastRun;
      const shown = run ? run.steps.filter((s) => s.dodge || s.gfi) : [];
      const f = hist[hist.length - 1];
      rolls.hidden = !shown.length && !shownZside;
      if (rolls.hidden) return;
      rolls.className = "st-rolls";
      if (!shown.length) {
        const act = f && f.act, W = dims && dims.W, H = dims && dims.H;
        const e = act && f.p[act] && zones(f, act[0] === "H" ? "A" : "H", W, H).get(sqKey(f.p[act]));
        rolls.hidden = !e;
        if (e) rolls.append(h("span", "st-roll bad", h("b", null, `${longName(act)} is marked by ${e.n === 1 ? "1 player" : e.n + " players"}`), " · moving away takes a dodge roll"));
        return;
      }
      rolls.append(h("span", "st-rolls-lbl", "Rolls"));
      for (const [s, kind] of shown.flatMap((s) => [s.dodge ? [s, "dodge"] : null, s.gfi ? [s, "gfi"] : null].filter(Boolean))) {
        const g = s[kind + "Roll"], need = kind === "dodge" ? s.need : s.gfiNeed;
        const last = g && g.rolls[g.rolls.length - 1];
        const what = kind === "dodge" ? `Dodge out of ${s.markers.length === 1 ? "1 tackle zone" : s.markers.length + " tackle zones"}` : "Go for it (extra square)";
        const into = kind === "dodge" && s.tzTo ? `, into ${s.tzTo} more` : "";
        const res = !g ? "" : g.rolls.map((r, i) => `${i ? ` → ${g.rr || "re-roll"}: ` : ""}${r.r === "" ? (r.ok ? "passed" : "failed") : r.r + (r.ok ? " ✓" : " ✗")}`).join("");
        const cls = !g ? "" : last.ok ? "ok" : "bad";
        rolls.append(h("span", `st-roll ${cls}`, h("b", null, `${need}+`), ` ${what}${into}`, g ? h("span", "st-roll-res", res + (g.fell ? " · falls" : !last.ok && s.pending ? " · re-roll?" : "")) : null));
      }
    }
    function renderPossess(f, game) {
      possess.innerHTML = "";
      const carrier = carrierOf(f);
      if (carrier) {
        const row = game.arena.board[1] || [];
        const goal = row.indexOf(carrier[0] === "H" ? "AWAY_TOUCHDOWN" : "HOME_TOUCHDOWN");
        const W = row.length - 2;
        const dist = goal >= 0 ? Math.abs(f.p[carrier][0] - goal) : null;
        const sideCls = carrier[0] === "H" ? "home" : "away";
        const fill = h("i");
        if (dist != null) {
          fill.style.width = `${((W - 1 - dist) / (W - 1)) * 100}%`;
          fill.style[goal <= 1 ? "right" : "left"] = "0";
        }
        possess.append(h("span", `st-pos-lbl ${sideCls}`, `● ${sideName(carrier)} has the ball`),
          h("span", "st-pos-sub", longName(carrier) + (dist == null ? "" : dist ? ` · ${dist} ${dist === 1 ? "square" : "squares"} from the end zone` : " · in the end zone")),
          h("span", `st-pos-meter ${sideCls}`, fill));
      } else if (f.ball) {
        possess.append(h("span", "st-pos-lbl", "○ The ball is loose"), h("span", "st-pos-sub", f.ball[2] === "a" ? "in the air" : "nobody has it"));
      } else {
        possess.append(h("span", "st-pos-lbl idle", "○ Ball not in play"));
      }
    }

    // ---- movement between frames ----
    function animate(els, ballEl, W, H, speed) {
      const n = hist.length - 1;
      if (n < 1 || reduceMotion || scored(hist[n - 1], hist[n])) return 0;
      const a = hist[n - 1], b = hist[n];
      const pos = (x, y) => ({ left: `${((x - 1) / W) * 100}%`, top: `${((y - 1) / H) * 100}%` });
      const sb = stepBall(a, b);
      // live play animates within the polling gap; replays scale with the chosen speed
      const per = Math.max(45, 150 / speed);
      let dur = 0;
      for (const m of stepMoves(a, b)) {
        const el = els[m.id]; if (!el) continue;
        if (m.kind === "place") { anims.push(el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 250 / speed })); continue; }
        const steps = m.pts.length - 1;
        let d = m.kind === "push" ? 260 / speed : Math.min(steps * per, 1600 / speed);
        let kf = m.pts.map((q, k) => ({ ...pos(q[0], q[1]), offset: steps ? k / steps : 1 }));
        const ease = m.kind === "push" ? "cubic-bezier(.2,.9,.3,1.15)" : "linear";
        if (m.kind === "walk" && lastRun && lastRun.act === m.id) {
          const rolled = (q0, q1) => lastRun.steps.some((st) => (st.dodge || st.gfi) && sameSq(st.from, q0) && sameSq(st.to, q1));
          const step = d / Math.max(1, steps), hold = 420 / Math.min(speed, 2.5);
          const times = [0], marks = [];
          for (let k = 1; k <= steps; k++) {
            const t = times[times.length - 1] + step;
            times.push(t);
            if (rolled(m.pts[k - 1], m.pts[k])) { marks.push({ k, t }); times.push(t + hold); }
          }
          const total = times[times.length - 1] || 1;
          kf = []; let ti = 0;
          for (let k = 0; k <= steps; k++) {
            kf.push({ ...pos(m.pts[k][0], m.pts[k][1]), offset: times[ti] / total }); ti++;
            const mk = marks.find((x) => x.k === k);
            if (mk) { kf.push({ ...pos(m.pts[k][0], m.pts[k][1]), offset: times[ti] / total }); ti++; }
          }
          d = total;
          // the roll's badge (and a fall, if any) appears as the runner lands on its square
          for (const el2 of layer.querySelectorAll(".tz-b, .tz-fell")) {
            const [f0, t0] = el2.dataset.step.split(">");
            const mk = marks.find((x) => sqKey(m.pts[x.k - 1]) === f0 && sqKey(m.pts[x.k]) === t0);
            if (mk) anims.push(el2.animate([{ opacity: 0, transform: "translateY(.15px)" }, { opacity: 1, transform: "none" }], { duration: 160, delay: mk.t, fill: "backwards" }));
          }
        }
        anims.push(el.animate(kf, { duration: d, easing: ease }));
        if (sb && sb.kind === "carried" && sb.by === m.id && ballEl) anims.push(ballEl.animate(kf, { duration: d, easing: ease }));
        dur = Math.max(dur, d);
      }
      if (sb && sb.kind !== "carried" && ballEl) {
        const [x0, y0] = sb.from, [x1, y1] = sb.to, lift = sb.kind === "flight" ? 1.2 : 0.45, N = 14, kf = [];
        for (let k = 0; k <= N; k++) {
          const t = k / N, arc = 4 * t * (1 - t);
          kf.push({ ...pos(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t - lift * arc), transform: `scale(${1 + 0.6 * arc})`, offset: t });
        }
        const d = (sb.kind === "flight" ? 650 : 450) / speed;
        anims.push(ballEl.animate(kf, { duration: d, easing: "ease-in-out", delay: Math.min(dur, 300 / speed) }));
        dur = Math.max(dur, d + Math.min(dur, 300 / speed));
      }
      return dur;
    }

    function showBanner(f) {
      const line = f.log.find((t) => /scored a Touchdown/.test(t));
      const who = line && line.match(/^([HA]\d+)/);
      banner.innerHTML = "";
      banner.append(h("b", null, "TOUCHDOWN"), h("span", null, who ? `${longName(who[1])} scores` : ""));
      banner.classList.add("on");
      clearTimeout(bannerTimer);
      bannerTimer = setTimeout(() => banner.classList.remove("on"), 2600);
    }

    // how: { contiguous: this frame directly follows the previous one, same: re-render of the same frame, speed }
    function render(game, how = {}) {
      anims.forEach((a) => a.cancel()); anims = [];
      lastGame = game;
      const prev = how.contiguous || how.same ? hist[hist.length - (how.same ? 2 : 1)] : null;
      const f = slim(game, prev);
      if (how.same && hist.length) hist[hist.length - 1] = f;
      else { if (!how.contiguous) hist = []; hist.push(f); if (hist.length > 120) hist.shift(); }
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
      renderPossess(f, game);

      // pitch
      pitch.style.aspectRatio = `${W} / ${H}`;
      pitch.style.backgroundImage = `url(${IMG}/arenas/pitch/${w}-${W}x${H}.jpg)`;
      pitch.style.setProperty("--ar", (W / H).toFixed(4));
      layer.innerHTML = "";
      const at = (x, y) => ({ left: `${((x - 1) / W) * 100}%`, top: `${((y - 1) / H) * 100}%`, width: `${100 / W}%`, height: `${100 / H}%` });
      const place = (el, x, y) => { Object.assign(el.style, at(x, y)); return el; };
      const under = svgLayer(W, H, "under"), over = svgLayer(W, H, "over");
      dims = { W, H };
      drawOverlays(under, over, f, H, W);
      layer.append(under);
      const activeId = st.active_player_id;
      const balls = (st.pitch && st.pitch.balls) || st.balls || [];
      const els = {};
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
          if (s.stunned) el.append(h("span", "st-status", "z"));
          el.setAttribute("aria-label", `${side === "home" ? "Home" : "Away"} #${p.nr} ${p.name}, ${p.role}`);
          layer.append(el);
          els[(side === "home" ? "H" : "A") + p.nr] = el;
        }
      }
      let ballEl = null;
      balls.filter((b) => b.position).forEach((b) => {
        ballEl = place(h("div", `st-ballsq ${b.is_carried ? "carried" : ""}`, h("span", `st-ball ${b.is_carried || b.on_ground ? "" : "air"}`)), b.position.x, b.position.y);
        layer.append(ballEl);
      });
      layer.append(over);
      pitch.append(hz);

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

      // what just happened, in plain English
      renderCaption();
      renderRolls();

      // who is acting right now, always visible (no hover needed)
      now.innerHTML = "";
      const act = !st.game_over && activeId && findPlayer(game, activeId);
      // kept on screen when nobody is acting, so the layout below doesn't move
      now.className = "st-now idle";
      delete now.dataset.pid;
      if (!act) now.append(h("span", "st-now-label", "Now"), h("span", null, st.game_over ? "Full time" : "No player acting"));
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
      const named = (t) => t.replace(/Home\(H\)/g, home.name).replace(/Away\(A\)/g, away.name);
      [...log].reverse().slice(0, 4).forEach((t, i) => ticker.append(h("li", i ? "" : "latest", named(t))));

      showCard();
      holdHeight();
      const n = hist.length - 1;
      if (n > 0 && !how.same && scored(hist[n - 1], f) && f.log.some((t) => /scored a Touchdown/.test(t))) showBanner(f);
      return how.same ? 0 : animate(els, ballEl, W, H, how.speed || 1);
    }

    // The stadium never shrinks while its width stays the same: whatever still varies (a dugout wrapping onto a
    // second row, say) only ever pushes the controls below down once, instead of up and down every frame.
    let heldWidth = 0, held = 0;
    function holdHeight() {
      const w = root.clientWidth;
      if (w !== heldWidth) { heldWidth = w; held = 0; root.style.minHeight = ""; }
      const ht = root.offsetHeight;
      if (ht > held) { held = ht; root.style.minHeight = ht + "px"; }
    }
    window.addEventListener("resize", () => { if (lastGame) holdHeight(); });

    // live mode: poll the match's latest state (the server answers 304 when nothing changed)
    let liveTimer = null;
    function live(matchId, onState) {
      stop();
      hist = [];
      const tick = () => fetch(`/api/matches/${matchId}/state`, { cache: "no-cache" }).then((r) => {
        if (!r.ok) throw new Error(r.status);
        const tag = r.headers.get("ETag");
        if (tag && tag === lastKey) return null;
        lastKey = tag;
        return r.json();
      }).then((g) => {
        // states arrive at most every 800ms, so moves are animated briskly
        if (g) { render(g, { contiguous: true, speed: 2.5 }); onState && onState(g); }
        if (!(g && g.state.game_over)) liveTimer = setTimeout(tick, 800);
      }).catch(() => { liveTimer = setTimeout(tick, 3000); });
      tick();
    }
    function stop() { clearTimeout(liveTimer); }

    // replay mode: fetch recorded frames (immutable, cached by the browser) with a small prefetch window.
    // Resolves to how long the frame's movement animation runs, so playback can wait for it.
    const cache = new Map();
    let wanted = null, shown = null;
    const fetchFrame = (matchId, i) => {
      if (!cache.has(i)) cache.set(i, fetch(`/api/matches/${matchId}/frames/${i}`).then((r) => r.json()));
      return cache.get(i);
    };
    function frame(matchId, i, total, speed) {
      wanted = i;
      const p = fetchFrame(matchId, i).then((g) => {
        if (wanted !== i) return 0;
        const how = { contiguous: shown !== null && i === shown + 1, same: i === shown, speed: speed || 1 };
        shown = i;
        return render(g, how);
      });
      for (let k = 1; k <= 4; k++) if (total === undefined || i + k < total) fetchFrame(matchId, i + k);
      for (const key of cache.keys()) if (Math.abs(key - i) > 40) cache.delete(key);
      return p;
    }

    return { render, live, stop, frame };
  }

  return { create };
})();
