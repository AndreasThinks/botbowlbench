"""
The MCP tool surface an LLM uses to play.

One :func:`build_mcp_server` call creates an MCP server bound to a single seat. High-level tools
(move, block, blitz, pass_ball, handoff, foul) are compiled into botbowl's primitive actions and
executed step by step; any decision that interrupts them (block dice, rerolls, push directions,
follow ups...) is reported back so the model can resolve it with ``take_action``.
"""
from typing import Optional

import anyio
from mcp.server.mcpserver import MCPServer

from botbowl.core.model import Action, Square
from botbowl.core.table import ActionType
from botbowl.core.pathfinding import get_all_paths

from bench import render
from bench.session import GameSession, Seat, fallback_action

RULES_PRIMER = """\
BLOOD BOWL QUICK RULES (5-a-side variant, 8 turns per half, 2 halves)
- Goal: score touchdowns by ending a player's action standing in the opponent's endzone holding the ball.
- On your turn, activate players one at a time. Each player may take ONE action: Move, Block, Blitz, Pass,
  Hand-off or Foul. Only one Blitz, one Pass, one Hand-off and one Foul per team turn.
- Move: up to MA squares, plus up to 2 "Go For It" squares (each needs a 2+ on a d6).
  Leaving a square that is adjacent to a standing opponent (a tackle zone) needs a Dodge roll (AG-based).
  Picking up the ball needs an AG roll.
- Block: hit an adjacent standing opponent. Compare strength (+1 per assisting team-mate): more ST means more
  dice. The side with more strength chooses the die. Results: Attacker Down, Both Down, Push, Defender
  Stumbles, Defender Down. Knocked-down players may be injured (armour, then injury roll).
- Blitz: move AND block in the same action (once per turn).
- Pass / Hand-off: throw the ball to a square / hand it to an adjacent team-mate who must catch it.
- Foul: kick a prone opponent (risk of being sent off).
- TURNOVER: if one of your players falls over (failed dodge/GFI, Attacker Down...), or a pass/hand-off/pickup
  fails, or the ball is lost, your turn ends immediately. Team re-rolls (limited, one per turn) can re-roll a
  failed roll.
- Stunned players spend a turn face-down; prone players can stand up (costs 3 MA).
TIPS: do safe actions first (blocks with 2 dice, moves without dodges), risky ones last; protect your ball
carrier with team-mates; don't leave the ball loose next to opponents.
"""


def _square(game, x, y) -> Optional[Square]:
    if x is None or y is None:
        return None
    try:
        return game.get_square(int(x), int(y))
    except Exception:
        return None


def _choice(game, action_type: ActionType):
    for a in game.state.available_actions:
        if a.action_type == action_type:
            return a
    return None


def is_trivial(game) -> Optional[Action]:
    """If the pending decision has exactly one possible action, return it."""
    actions = game.state.available_actions
    if len(actions) != 1:
        return None
    a = actions[0]
    if a.action_type in (ActionType.PLACE_PLAYER, ActionType.END_TURN):
        return None
    if len(a.positions) > 1 or len(a.players) > 1:
        return None
    return Action(a.action_type, position=a.positions[0] if a.positions else None,
                  player=a.players[0] if a.players else None)


class SeatTools:
    """Implements the tools for one seat. All methods are blocking and run in a worker thread."""

    def __init__(self, session: GameSession, seat: Seat, max_illegal: int = 3):
        self.session = session
        self.seat = seat
        self.game = session.game
        self.max_illegal = max_illegal
        self.consecutive_illegal = 0

    # ---- helpers ---------------------------------------------------------------------------
    @property
    def team(self):
        return self.seat.team

    def _not_ready(self) -> Optional[str]:
        if self.session.finished:
            return self._game_over_text()
        if not self.seat.is_pending():
            return "It is not your decision right now. Wait for your turn."
        return None

    def _game_over_text(self) -> str:
        opp = self.game.get_opp_team(self.team)
        return f"GAME OVER. Final score: you {self.team.state.score} - {opp.state.score} opponent."

    def _illegal(self, msg: str) -> str:
        self.consecutive_illegal += 1
        self.seat.counters["illegal_actions"] += 1
        self.session.log_event(self.seat.side, "illegal", {"text": msg})
        return "ILLEGAL: " + msg + "\n\n" + render.legal_actions_text(self.game, self.team)

    def _submit(self, action: Action) -> Optional[str]:
        """Validate and submit one primitive action. Returns an error string if it is not legal."""
        if not self.game._is_action_allowed(action):
            return f"{action.action_type.name} with player={render.pid(action.player) if action.player else None} " \
                   f"position={render.sq(action.position)} is not legal now."
        self.seat.submit(action)
        self._auto_resolve()
        return None

    def _auto_resolve(self):
        start = self.seat.decision
        while start is not None and not self.session.finished:
            d = self.seat.decision
            if d is None or d.key != start.key:
                return
            trivial = is_trivial(self.game)
            if trivial is None:
                return
            self.seat.counters["auto_resolved"] += 1
            self.seat.submit(trivial)

    def _outcome(self, report_idx: int, key: str, headline: str = "") -> str:
        lines = []
        if headline:
            lines.append(headline)
        events = render.events_since(self.game, report_idx, limit=25)
        if events:
            lines.append("What happened:\n  " + "\n  ".join(events))
        if self.session.finished:
            lines.append(self._game_over_text())
            return "\n".join(lines)
        d = self.seat.decision
        if d is None or d.key != key:
            lines.append("Your turn/phase is over. You will be given the new situation when you are next needed.")
            return "\n".join(lines)
        self.seat.report_cursor = len(self.game.state.reports)
        msgs = self.session.unread_messages(self.seat)
        if msgs:
            lines.append("Messages from your opponent: " + " | ".join(f'"{m}"' for m in msgs))
        lines.append(render.legal_actions_text(self.game, self.team))
        return "\n".join(lines)

    def _run(self, steps) -> str:
        """Run a list of callables that each return an Action (or None to skip) as one compound action."""
        err = self._not_ready()
        if err:
            return err
        key = self.seat.decision.key
        report_idx = len(self.game.state.reports)
        for i, step in enumerate(steps):
            if self.session.finished or self.seat.decision is None or self.seat.decision.key != key:
                break
            try:
                action = step()
            except ValueError as e:
                if i == 0:
                    return self._illegal(str(e))
                return self._outcome(report_idx, key, f"Stopped: {e}")
            if action is None:
                continue
            err = self._submit(action)
            if err:
                if i == 0:
                    return self._illegal(err)
                return self._outcome(report_idx, key, f"Stopped: {err}")
            # an interrupting decision (reroll, block dice, ...) -> hand control back to the model
            if not self.session.finished and self.seat.decision is not None and self.seat.decision.key == key \
                    and i < len(steps) - 1 and not self._expected_continuation():
                break
        self.consecutive_illegal = 0
        return self._outcome(report_idx, key)

    def _expected_continuation(self) -> bool:
        """True while the active player is still in its action procedure (i.e. no interrupting decision)."""
        types = {a.action_type for a in self.game.state.available_actions}
        return ActionType.END_PLAYER_TURN in types

    def _player(self, player_id, own=True):
        p = render.find_player(self.game, player_id)
        if p is None:
            raise ValueError(f"Unknown player '{player_id}'. Use ids like H3 or A5.")
        if own and p.team != self.team:
            raise ValueError(f"{player_id} is not one of your players.")
        if not own and p.team == self.team:
            raise ValueError(f"{player_id} is one of your own players.")
        return p

    def _start(self, player, action_type: ActionType):
        def step():
            active = self.game.state.active_player
            if active is not None and active == player and ActionType.END_PLAYER_TURN in \
                    {a.action_type for a in self.game.state.available_actions}:
                return None  # already active (e.g. continuing an interrupted action)
            choice = _choice(self.game, action_type)
            if choice is None or player not in choice.players:
                raise ValueError(f"{render.pid(player)} cannot start a "
                                 f"{action_type.name.replace('START_', '').lower()} action now.")
            return Action(action_type, player=player)
        return step

    def _at(self, action_type: ActionType, square_fn, label: str):
        def step():
            pos = square_fn()
            choice = _choice(self.game, action_type)
            if choice is None:
                raise ValueError(f"{label} is not possible now.")
            if pos not in choice.positions:
                raise ValueError(f"{label} to {render.sq(pos)} is not possible from here.")
            return Action(action_type, position=pos)
        return step

    def _end_player(self):
        def step():
            if _choice(self.game, ActionType.END_PLAYER_TURN) is not None:
                return Action(ActionType.END_PLAYER_TURN)
            return None
        return step

    # ---- info tools --------------------------------------------------------------------------
    def get_state(self) -> str:
        if self.session.finished:
            return self._game_over_text()
        txt = render.full_state_text(self.game, self.team)
        msgs = self.session.unread_messages(self.seat)
        if msgs:
            txt += "\n\nMessages from your opponent: " + " | ".join(f'"{m}"' for m in msgs)
        return txt

    def get_legal_actions(self) -> str:
        err = self._not_ready()
        return err or render.legal_actions_text(self.game, self.team)

    def get_player(self, player_id: str) -> str:
        try:
            p = render.find_player(self.game, player_id)
            if p is None:
                raise ValueError(f"Unknown player '{player_id}'.")
        except ValueError as e:
            return str(e)
        lines = [render.player_line(self.game, p)]
        if p.team == self.team and p.position is not None and p.state.up is not None and not p.state.used \
                and self.seat.is_pending():
            try:
                paths = get_all_paths(self.game, p)
                buckets = {}
                for path in paths:
                    end = path.steps[-1]
                    pct = int(round(path.prob * 100))
                    band = "100%" if pct >= 99 else (">=80%" if pct >= 80 else (">=50%" if pct >= 50 else "<50%"))
                    buckets.setdefault(band, []).append(f"({end.x},{end.y})")
                for band in ["100%", ">=80%", ">=50%", "<50%"]:
                    if band in buckets:
                        lines.append(f"Reachable with {band} success: " + " ".join(buckets[band]))
            except Exception as e:  # pathfinding is best-effort
                lines.append(f"(move options unavailable: {e})")
        if p.team != self.team and p.position is not None:
            mine = self.game.get_adjacent_players(p.position, team=self.team, down=False)
            if mine:
                lines.append("Adjacent to your: " + " ".join(render.pid(m) for m in mine))
        return "\n".join(lines)

    def send_message(self, text: str) -> str:
        return self.session.post_message(self.seat, text)

    # ---- action tools ------------------------------------------------------------------------
    def move(self, player_id: str, x: int, y: int) -> str:
        try:
            p = self._player(player_id)
        except ValueError as e:
            return self._illegal(str(e))
        target = _square(self.game, x, y)
        return self._run([self._start(p, ActionType.START_MOVE),
                          self._at(ActionType.MOVE, lambda: target, "Move"),
                          self._end_player()])

    def block(self, player_id: str, target_id: str) -> str:
        try:
            p = self._player(player_id)
            t = self._player(target_id, own=False)
        except ValueError as e:
            return self._illegal(str(e))
        return self._run([self._start(p, ActionType.START_BLOCK),
                          self._at(ActionType.BLOCK, lambda: t.position, f"Block on {target_id}")])

    def blitz(self, player_id: str, target_id: str, via_x: Optional[int] = None, via_y: Optional[int] = None) -> str:
        try:
            p = self._player(player_id)
            t = self._player(target_id, own=False)
        except ValueError as e:
            return self._illegal(str(e))
        steps = [self._start(p, ActionType.START_BLITZ)]
        if via_x is not None and via_y is not None:
            via = _square(self.game, via_x, via_y)
            steps.append(self._at(ActionType.MOVE, lambda: via, "Move"))
        steps.append(self._at(ActionType.BLOCK, lambda: t.position, f"Blitz on {target_id}"))
        return self._run(steps)

    def pass_ball(self, player_id: str, target_x: int, target_y: int,
                  move_to_x: Optional[int] = None, move_to_y: Optional[int] = None) -> str:
        try:
            p = self._player(player_id)
        except ValueError as e:
            return self._illegal(str(e))
        steps = [self._start(p, ActionType.START_PASS)]
        if move_to_x is not None and move_to_y is not None:
            dest = _square(self.game, move_to_x, move_to_y)
            steps.append(self._at(ActionType.MOVE, lambda: dest, "Move"))
        target = _square(self.game, target_x, target_y)
        steps.append(self._at(ActionType.PASS, lambda: target, "Pass"))
        return self._run(steps)

    def handoff(self, player_id: str, target_id: str,
                move_to_x: Optional[int] = None, move_to_y: Optional[int] = None) -> str:
        try:
            p = self._player(player_id)
            t = self._player(target_id)
        except ValueError as e:
            return self._illegal(str(e))
        steps = [self._start(p, ActionType.START_HANDOFF)]
        if move_to_x is not None and move_to_y is not None:
            dest = _square(self.game, move_to_x, move_to_y)
            steps.append(self._at(ActionType.MOVE, lambda: dest, "Move"))
        steps.append(self._at(ActionType.HANDOFF, lambda: t.position, f"Hand-off to {target_id}"))
        return self._run(steps)

    def foul(self, player_id: str, target_id: str) -> str:
        try:
            p = self._player(player_id)
            t = self._player(target_id, own=False)
        except ValueError as e:
            return self._illegal(str(e))
        return self._run([self._start(p, ActionType.START_FOUL),
                          self._at(ActionType.FOUL, lambda: t.position, f"Foul on {target_id}")])

    def end_turn(self) -> str:
        def step():
            if _choice(self.game, ActionType.END_TURN) is None:
                if _choice(self.game, ActionType.END_PLAYER_TURN) is not None:
                    raise ValueError("A player is mid-action: call take_action('END_PLAYER_TURN') first, "
                                     "or resolve the pending decision.")
                raise ValueError("You cannot end the turn right now.")
            return Action(ActionType.END_TURN)
        return self._run([step])

    def take_action(self, action_type: str, player_id: Optional[str] = None,
                    x: Optional[int] = None, y: Optional[int] = None) -> str:
        def step():
            name = (action_type or "").strip().upper()
            try:
                at = ActionType[name]
            except KeyError:
                raise ValueError(f"Unknown action_type '{action_type}'.")
            player = None
            if player_id:
                player = render.find_player(self.game, player_id)
                if player is None:
                    raise ValueError(f"Unknown player '{player_id}'.")
            pos = _square(self.game, x, y)
            if at == ActionType.END_SETUP and not self.game.is_setup_legal(self.team):
                raise ValueError("Setup is not legal yet: use a SETUP_FORMATION_* action first.")
            return Action(at, player=player, position=pos)
        return self._run([step])

    # ---- driver helpers ----------------------------------------------------------------------
    def force_fallback(self):
        """Submit the fallback action for the pending decision (used by the driver)."""
        if self.seat.is_pending() and not self.session.finished:
            self.seat.counters["forced_actions"] += 1
            self.seat.submit(fallback_action(self.game, self.team))


TOOL_DOCS = {
    "get_state": "Full situation: score, turn, board (ASCII grid), both rosters, recent opponent messages, "
                 "and the legal options for the pending decision.",
    "get_legal_actions": "Only the pending decision and its legal options.",
    "get_player": "Details for one player (e.g. 'H3'). For your own unused players it also lists reachable "
                  "squares grouped by the probability of getting there safely.",
    "move": "Activate one of your players with a Move action and move to (x,y) along the safest path; "
            "the player's action then ends.",
    "block": "Activate player_id with a Block action against an ADJACENT standing opponent target_id.",
    "blitz": "Blitz (once per turn): player_id moves next to target_id and blocks it. Optional via_x/via_y "
             "to move to a specific square first.",
    "pass_ball": "Pass action (once per turn): optionally move to (move_to_x,move_to_y) first (e.g. to pick "
                 "up the ball), then throw to square (target_x,target_y).",
    "handoff": "Hand-off (once per turn): optionally move first, then hand the ball to ADJACENT team-mate target_id.",
    "foul": "Foul (once per turn): player_id kicks a PRONE/STUNNED opponent target_id (moves next to it if needed).",
    "end_turn": "End your team turn. The call returns when it is your turn again (or the game is over).",
    "take_action": "Low-level: perform any legal action listed by get_legal_actions, e.g. "
                   "take_action('SELECT_DEFENDER_DOWN'), take_action('USE_REROLL'), take_action('PUSH', x=5, y=3), "
                   "take_action('SETUP_FORMATION_WEDGE'), take_action('END_SETUP'), take_action('HEADS'), "
                   "take_action('KICK'), take_action('PLACE_BALL', x=4, y=5), take_action('END_PLAYER_TURN').",
    "send_message": "Send a short chat message (max 280 chars, max 2 per turn) to your opponent. They will see "
                    "it on their next turn. Spectators see it too. Banter, bluffing and mind games are allowed.",
}


def build_mcp_server(tools: SeatTools) -> MCPServer:
    """Create an MCP server exposing the tools for one seat."""
    server = MCPServer("botbowl", instructions="Play Blood Bowl. " + RULES_PRIMER)

    def run(fn, *args):
        return anyio.to_thread.run_sync(lambda: fn(*args))

    @server.tool(description=TOOL_DOCS["get_state"])
    async def get_state() -> str:
        return await run(tools.get_state)

    @server.tool(description=TOOL_DOCS["get_legal_actions"])
    async def get_legal_actions() -> str:
        return await run(tools.get_legal_actions)

    @server.tool(description=TOOL_DOCS["get_player"])
    async def get_player(player_id: str) -> str:
        return await run(tools.get_player, player_id)

    @server.tool(description=TOOL_DOCS["move"])
    async def move(player_id: str, x: int, y: int) -> str:
        return await run(tools.move, player_id, x, y)

    @server.tool(description=TOOL_DOCS["block"])
    async def block(player_id: str, target_id: str) -> str:
        return await run(tools.block, player_id, target_id)

    @server.tool(description=TOOL_DOCS["blitz"])
    async def blitz(player_id: str, target_id: str, via_x: Optional[int] = None, via_y: Optional[int] = None) -> str:
        return await run(tools.blitz, player_id, target_id, via_x, via_y)

    @server.tool(description=TOOL_DOCS["pass_ball"])
    async def pass_ball(player_id: str, target_x: int, target_y: int,
                        move_to_x: Optional[int] = None, move_to_y: Optional[int] = None) -> str:
        return await run(tools.pass_ball, player_id, target_x, target_y, move_to_x, move_to_y)

    @server.tool(description=TOOL_DOCS["handoff"])
    async def handoff(player_id: str, target_id: str,
                      move_to_x: Optional[int] = None, move_to_y: Optional[int] = None) -> str:
        return await run(tools.handoff, player_id, target_id, move_to_x, move_to_y)

    @server.tool(description=TOOL_DOCS["foul"])
    async def foul(player_id: str, target_id: str) -> str:
        return await run(tools.foul, player_id, target_id)

    @server.tool(description=TOOL_DOCS["end_turn"])
    async def end_turn() -> str:
        return await run(tools.end_turn)

    @server.tool(description=TOOL_DOCS["take_action"])
    async def take_action(action_type: str, player_id: Optional[str] = None,
                          x: Optional[int] = None, y: Optional[int] = None) -> str:
        return await run(tools.take_action, action_type, player_id, x, y)

    @server.tool(description=TOOL_DOCS["send_message"])
    async def send_message(text: str) -> str:
        return await run(tools.send_message, text)

    return server
