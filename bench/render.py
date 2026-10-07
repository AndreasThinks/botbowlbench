"""
Turns botbowl game state into compact, LLM-readable text.

Everything is rendered from the perspective of one team ("you"). Players are referred to by
short ids: H<nr> for the home team and A<nr> for the away team (e.g. H3, A7).
"""
import os
import re
from typing import List, Optional

from botbowl.core.model import Square
from botbowl.core.table import ActionType, OutcomeType, Tile

_LOG_TEXTS = None


def _load_log_texts():
    """Reuse the web UI's report templates (services.js) so the LLM log matches what spectators see."""
    global _LOG_TEXTS
    if _LOG_TEXTS is not None:
        return _LOG_TEXTS
    path = os.path.join(os.path.dirname(__file__), "..", "botbowl", "web", "static", "js", "services.js")
    texts = {}
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        block = src.split("log_texts:", 1)[1].split("log_timouts:", 1)[0]
        for m in re.finditer(r"""['"]([A-Z_]+)['"]\s*:\s*(['"])(.*?)\2\s*,?\s*$""", block, re.M):
            texts[m.group(1)] = re.sub(r"</?b>", "", m.group(3))
    except (OSError, IndexError):
        pass
    _LOG_TEXTS = texts
    return texts


def pid(player) -> str:
    """Short player id, e.g. H3 or A7."""
    if player is None:
        return "?"
    home = getattr(player.team, "bench_side", "home") == "home"
    return ("H" if home else "A") + str(player.nr)


def find_player(game, short_id: str):
    """Resolve a short id (H3/A7, case-insensitive) or a raw botbowl player_id."""
    if short_id is None:
        return None
    s = str(short_id).strip()
    m = re.fullmatch(r"([HhAa])\s*#?\s*(\d+)", s)
    if m:
        team = game.state.home_team if m.group(1).upper() == "H" else game.state.away_team
        nr = int(m.group(2))
        for p in team.players:
            if p.nr == nr:
                return p
        return None
    return game.state.player_by_id.get(s)


def sq(pos: Optional[Square]) -> str:
    return "-" if pos is None else f"({pos.x},{pos.y})"


def is_home(game, team) -> bool:
    return team == game.state.home_team


def target_endzone_x(game, team) -> int:
    """The x column of the endzone this team must reach to score."""
    board = game.arena.board
    want = Tile.AWAY_TOUCHDOWN if is_home(game, team) else Tile.HOME_TOUCHDOWN
    for y in range(len(board)):
        for x in range(len(board[0])):
            if board[y][x] == want:
                return x
    return -1


def player_status(game, p) -> str:
    if p.position is None:
        return "off pitch"
    flags = []
    if p.state.stunned:
        flags.append("stunned")
    elif not p.state.up:
        flags.append("prone")
    else:
        flags.append("standing")
    if game.has_ball(p):
        flags.append("HAS BALL")
    if p.state.used:
        flags.append("used")
    return ", ".join(flags)


def player_line(game, p) -> str:
    skills = ",".join(s.name.replace("_", " ").title() for s in p.get_skills()) or "-"
    return (f"{pid(p)} {p.role.name:<8} MA{p.get_ma()} ST{p.get_st()} AG{p.get_ag()}+ AV{p.get_av()}+ "
            f"[{skills}] at {sq(p.position)} {player_status(game, p)}")


def board_text(game) -> str:
    arena = game.arena
    w, h = arena.width, arena.height
    ball_pos = game.get_ball_position()
    ball = game.get_ball()
    header = "     " + "".join(f"{x:>4}" for x in range(1, w - 1))
    lines = [header]
    for y in range(1, h - 1):
        row = f"{y:>4} "
        for x in range(1, w - 1):
            square = game.get_square(x, y)
            p = game.get_player_at(square)
            tile = arena.board[y][x]
            if p is not None:
                flag = " "
                if game.has_ball(p):
                    flag = "@"
                elif p.state.stunned:
                    flag = "z"
                elif not p.state.up:
                    flag = "_"
                cell = f"{pid(p)}{flag}"
            elif ball_pos is not None and ball_pos == square and ball is not None and not ball.is_carried:
                cell = "o" if ball.on_ground else "^"
            elif tile in (Tile.HOME_TOUCHDOWN, Tile.AWAY_TOUCHDOWN):
                cell = "#"
            else:
                cell = "."
            row += f"{cell:>4}"
        lines.append(row)
    return "\n".join(lines)


def legend(game, team) -> str:
    ex = target_endzone_x(game, team)
    you = "H (home)" if is_home(game, team) else "A (away)"
    opp = "A (away)" if is_home(game, team) else "H (home)"
    return (f"You control the {you} players; the opponent controls {opp}. "
            f"You score by carrying the ball into column x={ex} ('#' = endzone).\n"
            "Legend: '@' carrying ball, '_' prone, 'z' stunned, 'o' loose ball, '^' ball in the air. "
            "Coordinates are (x,y); x is the column, y the row.")


def scoreboard(game, team) -> str:
    opp = game.get_opp_team(team)
    turn = game.current_turn()
    whose = "-"
    if turn is not None:
        whose = "YOUR TURN" if turn.team == team else "opponent's turn"
        if turn.blitz:
            whose += " (kick-off Blitz!)"
        if turn.quick_snap:
            whose += " (Quick Snap)"
    return (f"Score: you {team.state.score} - {opp.state.score} opponent | Half {game.state.half} | "
            f"Your turn #{team.state.turn}/{game.config.rounds}, opponent turn #{opp.state.turn} | {whose}\n"
            f"Team rerolls left: you {team.state.rerolls} (used this turn: {'yes' if team.state.reroll_used else 'no'}), "
            f"opponent {opp.state.rerolls} | Weather: {game.state.weather.name.replace('_', ' ').title()}")


def roster_text(game, team, title: str) -> str:
    lines = [title]
    on_pitch = [p for p in team.players if p.position is not None]
    for p in sorted(on_pitch, key=lambda p: p.nr):
        lines.append("  " + player_line(game, p))
    reserves = game.get_reserves(team)
    kod = game.get_knocked_out(team)
    cas = game.get_casualties(team)
    if reserves:
        lines.append("  Reserves: " + ", ".join(f"{pid(p)} {p.role.name}" for p in reserves))
    if kod:
        lines.append("  Knocked out: " + ", ".join(pid(p) for p in kod))
    if cas:
        lines.append("  Casualties: " + ", ".join(pid(p) for p in cas))
    return "\n".join(lines)


def report_text(game, report) -> Optional[str]:
    texts = _load_log_texts()
    name = report.outcome_type.name
    tpl = texts.get(name)
    if tpl is None:
        return None
    team_name = None
    if report.team is not None:
        team_name = "Home(H)" if is_home(game, report.team) else "Away(A)"
    line = tpl
    line = line.replace("<home_team>", "Home(H)").replace("<away_team>", "Away(A)")
    if team_name:
        line = line.replace("<team>", team_name)
    n = report.n
    if not isinstance(n, int):
        n = str(getattr(n, "name", n)).replace("NONE", "badly hurt").lower()
    line = line.replace("<n>", str(n))
    if report.skill is not None:
        line = line.replace("<skill>", report.skill.name.replace("_", " ").title())
    if report.player is not None:
        line = line.replace("<players>", pid(report.player) + "'s").replace("<player>", pid(report.player))
    if report.opp_player is not None:
        line = line.replace("<opp_player>", pid(report.opp_player))
    if report.rolls:
        rolls = []
        for r in report.rolls:
            try:
                vals = r.get_values()
                rolls.append("/".join(str(getattr(v, "name", v)).replace("BlockDieValue.", "") for v in vals))
            except Exception:
                pass
        if rolls:
            line += f" [roll {' '.join(rolls)}]"
    line = re.sub(r"<[^>]+>", "", line)
    return re.sub(r"\s+", " ", line).strip()


_QUIET = {OutcomeType.PLAYER_PLACED, OutcomeType.PLAYER_READY, OutcomeType.PLAYER_NOT_READY,
          OutcomeType.END_PLAYER_TURN}


def events_since(game, start_idx: int, limit: int = 40) -> List[str]:
    out = []
    for r in game.state.reports[start_idx:]:
        if r.outcome_type in _QUIET:
            continue
        t = report_text(game, r)
        if t:
            out.append(t)
    if len(out) > limit:
        out = [f"... ({len(out) - limit} earlier events omitted)"] + out[-limit:]
    return out


# Spectator log: the same lines, tagged so the UI can highlight what matters.
_TAGS = {OutcomeType.TOUCHDOWN: "td", OutcomeType.TURNOVER: "turnover", OutcomeType.KNOCKED_OUT: "injury",
         OutcomeType.CASUALTY: "injury", OutcomeType.CASUALTY_APOTHECARY: "injury",
         OutcomeType.STUNNED: "down", OutcomeType.KNOCKED_DOWN: "down",
         OutcomeType.FUMBLE: "fail", OutcomeType.INTERCEPTION: "fail"}


def log_lines(game, start_idx: int) -> List[dict]:
    """Game reports from start_idx on as [{"t": text, "k": tag}]; tag is "" for routine lines."""
    out = []
    for r in game.state.reports[start_idx:]:
        if r.outcome_type in _QUIET or r.outcome_type == OutcomeType.TURN_START:   # the UI draws turn headers
            continue
        t = report_text(game, r)
        if not t:
            continue
        tag = _TAGS.get(r.outcome_type) or ("fail" if r.outcome_type.name.startswith("FAILED_") else "")
        out.append({"t": t, "k": tag})
    return out


# --- Legal actions -------------------------------------------------------------------------

ACTION_HELP = {
    "SELECT_ATTACKER_DOWN": "Attacker Down: your blocker is knocked down (TURNOVER)",
    "SELECT_BOTH_DOWN": "Both Down: both players fall unless they have Block (TURNOVER if your player falls)",
    "SELECT_PUSH": "Push: defender pushed back one square",
    "SELECT_DEFENDER_STUMBLES": "Defender Stumbles: defender pushed and knocked down unless it has Dodge",
    "SELECT_DEFENDER_DOWN": "Defender Down: defender pushed and knocked down",
    "USE_REROLL": "spend a team re-roll to re-roll the roll shown above (block dice or failed roll)",
    "DONT_USE_REROLL": "keep the result shown above",
    "USE_SKILL": "use the skill offered",
    "DONT_USE_SKILL": "don't use the skill",
    "HEADS": "call heads", "TAILS": "call tails",
    "KICK": "kick off this half", "RECEIVE": "receive the ball this half",
    "END_SETUP": "finish placing players (setup must be legal)",
    "END_TURN": "end your team turn",
    "END_PLAYER_TURN": "end the active player's action",
    "STAND_UP": "stand the active player up",
    "PLACE_BALL": "choose where to kick the ball (opponent's half)",
    "SELECT_PLAYER": "choose a player (e.g. to receive a touchback)",
    "SELECT_NONE": "choose no player",
    "PUSH": "choose the square the defender is pushed to",
    "FOLLOW_UP": "choose to follow up (move into the vacated square) or stay",
    "USE_APOTHECARY": "use the apothecary", "DONT_USE_APOTHECARY": "don't use the apothecary",
    "USE_BRIBE": "use a bribe", "DONT_USE_BRIBE": "don't use a bribe",
}


def _positions_text(positions, max_items=24) -> str:
    pts = [p for p in positions if p is not None]
    if len(pts) <= max_items:
        return " ".join(sq(p) for p in pts)
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return f"{len(pts)} squares within x={min(xs)}..{max(xs)}, y={min(ys)}..{max(ys)}"


def block_dice_text(n) -> str:
    if n is None:
        return "?"
    if n > 0:
        return f"{n}d you pick"
    return f"{-n}d opp picks"


def adjacent_block_targets(game, player, team):
    out = []
    if player.position is None:
        return out
    for opp in game.get_adjacent_players(player.position, team=game.get_opp_team(team), down=False):
        try:
            dice = game.num_block_dice(player, opp)
        except Exception:
            dice = None
        out.append(f"{pid(opp)}({block_dice_text(dice)})")
    return out


def legal_actions_text(game, team) -> str:
    """Describe the pending decision and every legal option, grouped for readability."""
    actions = game.state.available_actions
    if not actions:
        return "No decision pending."
    proc = game.state.stack.peek()
    proc_name = type(proc).__name__
    types = {a.action_type for a in actions}
    lines = []

    starters = {ActionType.START_MOVE: "move", ActionType.START_BLOCK: "block", ActionType.START_BLITZ: "blitz",
                ActionType.START_PASS: "pass", ActionType.START_HANDOFF: "handoff", ActionType.START_FOUL: "foul"}
    if types & set(starters.keys()):
        lines.append("Decision: choose a player to activate (each player can take one action per turn).")
        by_player = {}
        for a in actions:
            if a.action_type in starters:
                for p in a.players:
                    by_player.setdefault(p, []).append(starters[a.action_type])
        for p in sorted(by_player, key=lambda p: p.nr):
            opts = by_player[p]
            extra = ""
            if "block" in opts:
                extra = " | block targets: " + " ".join(adjacent_block_targets(game, p, team))
            lines.append(f"  {pid(p)} at {sq(p.position)}{' (prone)' if not p.state.up else ''}: "
                         f"{', '.join(opts)}{extra}")
        turn = game.current_turn()
        if turn is not None:
            avail = []
            for k, v in (("blitz", turn.blitz_available), ("pass", turn.pass_available),
                         ("handoff", turn.handoff_available), ("foul", turn.foul_available)):
                avail.append(f"{k}:{'available' if v else 'used'}")
            lines.append("  Once-per-turn actions -> " + ", ".join(avail))
        lines.append("  Or END_TURN.")
        lines.append("Tools: move / block / blitz / pass_ball / handoff / foul / end_turn.")
        return "\n".join(lines)

    active = game.state.active_player
    if active is not None and proc_name.endswith("Action"):
        lines.append(f"Decision: {pid(active)} is mid-{proc_name.replace('Action', '').upper()} action "
                     f"at {sq(active.position)} (moves used {active.state.moves}/{active.get_ma()}+2 GFI).")
    else:
        lines.append(f"Decision ({proc_name}):")
    for a in actions:
        name = a.action_type.name
        if name == "PLACE_PLAYER":
            lines.append("  PLACE_PLAYER: manual placement (prefer a SETUP_FORMATION_* option)")
            continue
        desc = ACTION_HELP.get(name, "")
        if name.startswith("SETUP_FORMATION_"):
            desc = "auto-place your players in the " + name.replace("SETUP_FORMATION_", "").lower() + " formation"
        detail = ""
        if name == "MOVE":
            detail = f" reachable: {_positions_text(a.positions, 12)}"
        elif name in ("BLOCK", "FOUL", "HANDOFF") and a.positions:
            tgts = []
            for i, pos in enumerate(a.positions):
                pl = game.get_player_at(pos)
                extra = ""
                if name == "BLOCK" and a.block_dice and i < len(a.block_dice):
                    extra = f"({block_dice_text(a.block_dice[i])})"
                tgts.append(f"{pid(pl) if pl else ''}{sq(pos)}{extra}")
            detail = " targets: " + " ".join(tgts)
        elif name == "PASS" and a.positions:
            detail = f" target squares: {_positions_text(a.positions, 12)}"
        elif a.players:
            detail = " players: " + " ".join(pid(p) for p in a.players)
        elif a.positions:
            detail = " squares: " + _positions_text(a.positions)
        lines.append(f"  {name}{(' - ' + desc) if desc else ''}{detail}")
    lines.append("Use take_action(action_type, player_id?, x?, y?) for these.")
    return "\n".join(lines)


def full_state_text(game, team, include_board=True) -> str:
    parts = [scoreboard(game, team)]
    if include_board:
        parts.append(legend(game, team))
        parts.append(board_text(game))
    parts.append(roster_text(game, team, "YOUR PLAYERS:"))
    parts.append(roster_text(game, game.get_opp_team(team), "OPPONENT PLAYERS:"))
    parts.append(legal_actions_text(game, team))
    return "\n\n".join(parts)
