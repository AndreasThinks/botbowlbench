// BotBowl Bench - how a model's thinking is drawn, on the match page and the homepage.
// One look everywhere: a thought is a single line (how long it took, as a number and a bar, then a headline) that
// opens to the full text; a reply still streaming is the same line, animated, with its bar filling toward the
// point where the reply is cut.
const BBThink = (() => {
  const { h } = BB;
  const SCALE = 120;   // seconds a full duration bar stands for: the default reply limit (protocol 1.6)
  const SHORT = 160;   // thoughts shorter than this are narration ("Good. Now placing A6 at (3,4).")
  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const ktok = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n);

  // GPT and Claude summaries arrive as "**Title**\n\nbody**Next title**\n\nbody": split them into headed sections
  function sections(text) {
    const t = (text || "").trim(), re = /\*\*([^*\n]{3,90})\*\*/g, out = [];
    let m, last = 0, cur = null;
    while ((m = re.exec(t))) {
      const before = t.slice(last, m.index).trim();
      if (cur) cur.body = before; else if (before) out.push({ title: null, body: before });
      cur = { title: m[1].trim(), body: "" };
      out.push(cur);
      last = re.lastIndex;
    }
    const rest = t.slice(last).trim();
    if (cur) cur.body = rest; else if (rest) out.push({ title: null, body: rest });
    return out;
  }
  const plain = (t) => (t || "").replace(/\*\*/g, "").replace(/\s+/g, " ").trim();
  const sentences = (t) => plain(t).split(/(?<=[.!?])\s+(?=[A-Z0-9(])/).filter((s) => s.length > 2);
  const clip = (s) => s.length > 140 ? s.slice(0, 137).replace(/\s\S*$/, "") + "…" : s;

  // a finished thought's headline: its first summary heading, else its first sentence that says something
  // (skipping "Great." openers). A live one: the latest heading, else the latest finished sentence of any length.
  function headline(text, live) {
    const titled = sections(text).filter((s) => s.title);
    if (titled.length) return titled[live ? titled.length - 1 : 0].title;
    const ss = sentences(text);
    if (!ss.length) return clip(plain(text));
    if (!live) return clip(ss.find((x) => x.length >= 30) || ss[0]);
    const done = ss.filter((x, i) => i < ss.length - 1 || /[.!?]$/.test(x));
    return clip([...done].reverse().find((x) => x.length >= 40) || done[done.length - 1] || ss[ss.length - 1]);
  }

  // the readable body: summary headings as small headings, paragraphs as paragraphs
  function body(text, skip) {
    const out = [];
    sections(text).forEach((s) => {
      // the heading used as the line's headline isn't repeated
      if (s.title && s.title !== skip) out.push(h("div", { class: "th-h" }, s.title));
      for (const p of (s.body || "").split(/\n{2,}/)) if (p.trim()) out.push(h("p", { class: "th-p" }, p.replace(/\*\*/g, "").trim()));
    });
    return out;
  }
  // live text: only the newest part, cut back to a word boundary
  function tail(text, n) {
    const t = plain(text);
    return t.length <= n ? t : "…" + t.slice(-n).replace(/^\S*\s/, "");
  }

  function durBar(secs, cut) {
    const pct = cut ? 100 : secs == null ? 0 : Math.min(100, (secs / SCALE) * 100);
    return h("span", { class: `th-dur ${secs == null ? "none" : ""}`, title: secs == null ? null : `${mmss(secs)} of thinking (the bar is full at ${mmss(SCALE)})` },
      h("span", { class: "th-time" }, secs == null ? "–" : mmss(secs)),
      h("i", {}, h("b", { style: { width: Math.max(secs == null ? 0 : 3, pct) + "%" } })));
  }

  // One finished thought. `secs`: payload.secs, or for games logged before thoughts carried it, the gap since the
  // previous event (about the reply's own time).
  function line(e, secs, opts = {}) {
    const p = e.payload || {}, text = p.text || "";
    if (p.secs != null) secs = p.secs;
    const short = text.length < SHORT && !p.cut;
    const el = h("div", { class: `thought th-line ${short ? "short" : ""} ${p.cut ? "cut" : ""}` });
    if (short) {
      el.append(h("span", { class: "th-ic" }), durBar(secs), h("span", { class: "th-head" }, plain(text)));
      return el;
    }
    const hd = headline(text, false);
    const tokens = p.tokens != null ? `${ktok(p.tokens)} tokens` : p.tokens_est != null ? `≈${ktok(p.tokens_est)} tokens` : null;
    const row = h("button", { type: "button", class: "th-row", "aria-expanded": "false" },
      h("span", { class: "th-ic" }), durBar(secs, p.cut),
      h("span", { class: "th-head" }, p.cut ? h("span", { class: "th-cut" }, "Cut off · ") : null, hd),
      h("span", { class: "th-chev", "aria-hidden": "true" }, "▸"));
    const more = h("div", { class: "th-body" }, ...body(text, hd),
      h("div", { class: "th-foot" }, [secs != null ? `${mmss(secs)} of thinking` : null, tokens].filter(Boolean).join(" · "),
        p.cut ? h("div", { class: "th-cutnote" }, "The reply was still running at its time limit, so it was stopped and this thinking was handed back to the model.") : null));
    row.onclick = (ev) => { ev.stopPropagation(); row.setAttribute("aria-expanded", String(el.classList.toggle("open"))); };
    if (opts.open) { el.classList.add("open"); row.setAttribute("aria-expanded", "true"); }
    el.append(row, more);
    return el;
  }

  // A reply still coming in. x = the server's waiting entry {since, turn_ends, cut_at, chars, text}.
  function live(side, x, now, name, opts = {}) {
    const t = Math.max(0, now - x.since);
    const cutIn = x.cut_at != null ? Math.max(1, x.cut_at - x.since) : SCALE;
    const left = x.turn_ends != null ? Math.max(0, x.turn_ends - now) : null;
    const est = x.chars ? Math.round(x.chars / 4) : 0;
    const quiet = !x.text;
    return h("div", { class: `th-live ${side} ${opts.big ? "big" : ""}` },
      h("div", { class: "th-row" }, h("span", { class: "th-spin" }), h("b", { class: "th-who" }, name),
        h("span", { class: "th-time" }, `thinking ${mmss(t)}`)),
      h("div", { class: `th-head ${quiet ? "quiet" : ""}` }, quiet ? "Keeps its thinking private" : headline(x.text, true)),
      h("div", { class: "th-track", title: `The reply is stopped at ${mmss(cutIn)} and its thinking handed back` },
        h("b", { style: { width: Math.min(100, (t / cutIn) * 100) + "%" } })),
      quiet ? null : h("div", { class: "th-tail" }, h("span", {}, tail(x.text, opts.big ? 900 : 240))),
      h("div", { class: "th-foot" }, [quiet ? "its summary appears when it acts" : `≈${ktok(est)} tokens`,
        `cut at ${mmss(cutIn)}`, left != null ? `${mmss(left)} left this turn` : null].filter(Boolean).join(" · ")));
  }

  // the one-line version for the homepage LIVE bar
  function chip(side, x, now, name) {
    return h("span", { class: `th-chip ${side}` }, h("span", { class: "th-spin" }), h("b", {}, name), ` thinking ${mmss(Math.max(0, now - x.since))}`);
  }

  return { line, live, chip, headline, mmss };
})();
