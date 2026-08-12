"""Power.log parser for Hearthstone Battlegrounds opponent-board tracking.

Feed lines from Power.log to BgGame.feed_line(). The game keeps a
last-seen board snapshot per lobby player, taken at the start of each
combat phase (before any attacks resolve).

Run standalone to replay a finished log and print every snapshot:

    python parser.py "R:/Games/Hearthstone/Logs/<dir>/Power.log" [--debug]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Only the GameState stream is parsed; PowerTaskList duplicates it.
RE_LINE = re.compile(
    r"^D (?P<ts>[\d:.]+) GameState\.(?P<method>DebugPrintPower|DebugPrintGame)\(\) -\s?(?P<body>.*)$"
)
RE_CREATE_GAME = re.compile(r"^CREATE_GAME$")
RE_PLAYER_NAME = re.compile(r"^PlayerID=(?P<id>\d+), PlayerName=(?P<name>.+)$")
RE_CREATE_PLAYER = re.compile(
    r"^Player EntityID=(?P<eid>\d+) PlayerID=(?P<pid>\d+) GameAccountId=\[hi=(?P<hi>\d+) lo=(?P<lo>\d+)\]$"
)
RE_FULL_ENTITY = re.compile(r"^FULL_ENTITY - (?:Creating|Updating) (?P<ref>.+?) CardID=(?P<card>.*)$")
RE_SHOW_ENTITY = re.compile(r"^SHOW_ENTITY - Updating (?P<ref>.+?) CardID=(?P<card>.*)$")
RE_CHANGE_ENTITY = re.compile(r"^CHANGE_ENTITY - Updating Entity=(?P<ref>.+?) CardID=(?P<card>.*)$")
RE_TAG_CHANGE = re.compile(r"^TAG_CHANGE Entity=(?P<ref>.+?) tag=(?P<tag>\S+) value=(?P<value>\S+)")
RE_TAG_LINE = re.compile(r"^tag=(?P<tag>\S+) value=(?P<value>\S+)$")
RE_BRACKET_REF = re.compile(
    r"\[entityName=(?P<name>.*?) id=(?P<id>\d+) zone=\S+ zonePos=\d+ cardId=(?P<card>\S*) player=\d+\]"
)
RE_ID_ONLY = re.compile(r"^(?:ID|Entity)=(?P<id>\d+)$")

HERO_CARD_PREFIXES = ("TB_BaconShop_HERO", "BG")  # heroes checked via CARDTYPE anyway
INNKEEPER_CARDS = {"TB_BaconShop_HERO_PH", "TB_BaconShopBob", "TB_BaconShop_HERO_KelThuzad"}

KEYWORD_TAGS = {
    "TAUNT": "Taunt",
    "DIVINE_SHIELD": "Divine Shield",
    "POISONOUS": "Poisonous",
    "VENOMOUS": "Venomous",
    "REBORN": "Reborn",
    "WINDFURY": "Windfury",
    "STEALTH": "Stealth",
    "DEATHRATTLE": "Deathrattle",
}


@dataclass
class Entity:
    id: int
    card_id: str = ""
    name: str = ""
    tags: dict = field(default_factory=dict)

    def tag(self, name, default=0):
        return self.tags.get(name, default)


@dataclass
class Minion:
    card_id: str
    name: str
    attack: int
    health: int
    golden: bool
    keywords: list

    def label(self):
        parts = [self.name or self.card_id]
        if self.golden:
            parts.append("(Golden)")
        parts.append(f"{self.attack}/{self.health}")
        if self.keywords:
            parts.append(", ".join(self.keywords))
        return "  ".join(parts)


@dataclass
class Snapshot:
    """Board of one lobby player, as seen in one combat."""

    player_id: int
    hero_card_id: str
    hero_name: str
    round_num: int
    minions: list = field(default_factory=list)
    tech_level: int = 0
    health: int = 0  # fighter's hp at combat start
    armor: int = 0
    trinkets: list = field(default_factory=list)  # card ids
    hero_power: str = ""  # card id
    buddy_dbf: int = 0  # BACON_COMPANION_ID of the fighter's hero, if any
    # Outcome of that combat from OUR perspective, filled in after it ends:
    # "" (unknown) | "win" | "loss" | "tie";  result_dmg = hp swing
    result: str = ""
    result_dmg: int = 0


class BgGame:
    """Incremental state machine over Power.log lines."""

    def __init__(self, on_snapshot=None, on_new_game=None, debug=False):
        self.on_snapshot = on_snapshot  # callback(Snapshot)
        self.on_new_game = on_new_game  # callback()
        self.debug = debug
        self.learned_names: dict[str, str] = {}  # card_id -> name, survives games
        self._reset()

    def _reset(self):
        # A reconnect mid-game produces a fresh CREATE_GAME whose first TURN
        # value is > 1; stash per-player data so it can be carried over.
        carry = None
        if getattr(self, "is_battlegrounds", False) and not getattr(self, "game_over", True):
            carry = {
                "history": self.history,
                "snapshots": self.snapshots,
                "names": self.player_names,
                "hp_track": self.hp_track,
            }
        self._carryover = carry
        self._first_turn_seen = False
        self.entities: dict[int, Entity] = {}
        self.player_names: dict[int, str] = {}  # lobby player id -> battletag
        self.friendly_controller = 0
        self.enemy_controller = 0
        self.teammate_id = 0  # duos: lobby id of our partner
        self.next_opponent_id = 0
        self.turn = 0
        self.is_duos = False
        # Lobby id of the opponent whose warband is being set up this combat;
        # set by BACON_CURRENT_COMBAT_PLAYER_ID, consumed at STEP=MAIN_START.
        self._pending_combat_opponent = 0
        # Duos: mid-combat the fallen fighter's partner steps in. The same
        # tag announces them; we snapshot once their board is placed. The
        # friendly side alternates fighters too — when our teammate fights,
        # their board appears on our side and we snapshot it the same way.
        self._in_combat = False
        self._cycle_started = False  # MAIN_START seen since last MAIN_READY
        self._last_combat_pid = 0
        self._pending_swap = 0
        self._pending_combat_friendly = 0
        self._last_friendly_pid = 0
        self._pending_friendly_swap = 0
        self.snapshots: dict[int, Snapshot] = {}  # latest per player
        self.history: dict[int, list] = {}  # all snapshots per player, in order
        self.hp_track: list[tuple[int, int]] = []  # (round, our effective hp)
        self.anomaly_dbf = 0
        self._combat_snaps: list[Snapshot] = []  # taken during current combat
        self._combat_pre = None  # (our_hp, enemy_hp) at combat start
        self.is_battlegrounds = False
        self._pending_entity: Entity | None = None
        self.game_over = False

    # ------------------------------------------------------------------ input

    def feed_line(self, line: str):
        m = RE_LINE.match(line)
        if not m:
            return
        body = m.group("body")
        if m.group("method") == "DebugPrintGame":
            self._on_game_meta(body.strip())
            return
        self._on_power(body)

    def _on_game_meta(self, body: str):
        if body.startswith("GameType="):
            self.is_battlegrounds = "BATTLEGROUNDS" in body
            self.is_duos = "DUO" in body
            return
        m = RE_PLAYER_NAME.match(body)
        if m:
            self.player_names[int(m.group("id"))] = m.group("name")

    def _on_power(self, body: str):
        stripped = body.strip()

        if RE_CREATE_GAME.match(stripped):
            self._reset()
            if self.on_new_game:
                self.on_new_game()
            return

        m = RE_CREATE_PLAYER.match(stripped)
        if m:
            pid = int(m.group("pid"))
            if int(m.group("hi")) != 0 and not self.friendly_controller:
                self.friendly_controller = pid
            else:
                self.enemy_controller = pid
            ent = self._entity(int(m.group("eid")))
            ent.tags["PLAYER_ID"] = pid
            self._pending_entity = ent  # following tag lines belong to it
            return

        m = RE_FULL_ENTITY.match(stripped) or RE_SHOW_ENTITY.match(stripped)
        if m:
            ent = self._resolve(m.group("ref"))
            if ent is not None:
                card = m.group("card").strip()
                if card:
                    ent.card_id = card
                self._pending_entity = ent
            return

        m = RE_CHANGE_ENTITY.match(stripped)
        if m:
            ent = self._resolve(m.group("ref"))
            if ent is not None:
                card = m.group("card").strip()
                if card:
                    ent.card_id = card
            self._pending_entity = None
            return

        m = RE_TAG_CHANGE.match(stripped)
        if m:
            self._pending_entity = None
            ent = self._resolve(m.group("ref"))
            self._apply_tag(ent, m.group("tag"), m.group("value"))
            return

        m = RE_TAG_LINE.match(stripped)
        if m and self._pending_entity is not None:
            self._apply_tag(self._pending_entity, m.group("tag"), m.group("value"))
            return

        # A swapped-in or late-placed board is fully placed by the time
        # attacks happen.
        if (
            (self._pending_swap or self._pending_friendly_swap)
            and stripped.startswith("BLOCK_START")
            and "BlockType=ATTACK" in stripped
        ):
            self._flush_pending_swaps()

        # Any other line (BLOCK_START, META_DATA, ...) ends a pending entity.
        self._pending_entity = None

    # ------------------------------------------------------------- entity ref

    def _resolve(self, ref: str) -> Entity | None:
        ref = ref.strip()
        m = RE_ID_ONLY.match(ref)
        if m:
            return self._entity(int(m.group("id")))
        m = RE_BRACKET_REF.search(ref)
        if m:
            ent = self._entity(int(m.group("id")))
            if m.group("name") and m.group("name") != "UNKNOWN ENTITY":
                ent.name = m.group("name")
                if m.group("card"):
                    self.learned_names[m.group("card")] = ent.name
            if m.group("card"):
                ent.card_id = m.group("card")
            return ent
        if ref.isdigit():
            return self._entity(int(ref))
        # GameEntity or a player name — track GameEntity as id 1 by convention;
        # player-name refs (e.g. "Kreativ#2118") carry NEXT_OPPONENT_PLAYER_ID.
        if ref == "GameEntity":
            return self._entity(1)
        return self._entity_by_name(ref)

    def _entity(self, eid: int) -> Entity:
        ent = self.entities.get(eid)
        if ent is None:
            ent = Entity(eid)
            self.entities[eid] = ent
        return ent

    def _entity_by_name(self, name: str) -> Entity | None:
        for ent in self.entities.values():
            if ent.name == name:
                return ent
        # Unseen named entity (usually a player) — give it a synthetic slot.
        ent = Entity(-len(self.entities) - 1000, name=name)
        self.entities[ent.id] = ent
        return ent

    # ------------------------------------------------------------------- tags

    def _apply_tag(self, ent: Entity | None, tag: str, value: str):
        if ent is None:
            return
        ent.tags[tag] = self._intval(value)
        if tag == "NEXT_OPPONENT_PLAYER_ID":
            self.next_opponent_id = int(value)
        elif tag == "BACON_CURRENT_COMBAT_PLAYER_ID":
            pid = int(value)
            # Both player entities get this tag; the enemy seat's value names
            # the lobby player we are fighting, and the enemy seat entity is
            # renamed to that player's battletag just before the tag lands.
            if pid > 0 and self.friendly_controller and pid in (
                self.friendly_controller,
                self.teammate_id,
            ):
                if self._in_combat:
                    if pid != self._last_friendly_pid:
                        self._pending_friendly_swap = pid
                elif self._cycle_started:
                    # Announced after MAIN_START (tag order varies by patch);
                    # capture via the deferred path.
                    self._last_friendly_pid = pid
                    self._pending_friendly_swap = pid
                else:
                    self._pending_combat_friendly = pid
            elif pid > 0 and self.friendly_controller:
                if self._in_combat:
                    if pid != self._last_combat_pid:
                        self._pending_swap = pid
                elif self._cycle_started:
                    # Combat announced after MAIN_START — enter combat mode
                    # now; the board snapshot happens when attacks begin.
                    self._in_combat = True
                    self._last_combat_pid = pid
                    self._combat_snaps = []
                    self._combat_pre = (
                        self._pid_hp(self.friendly_controller),
                        self._pid_hp(pid),
                    )
                    self._pending_swap = pid
                else:
                    self._pending_combat_opponent = pid
                if (
                    ent.name
                    and "UNKNOWN" not in ent.name
                    and not ent.card_id  # player entities have no card; skips Bob
                    # In duos our own seat gets tagged with the teammate's id;
                    # never record our own battletag for another player.
                    and ent.name != self.player_names.get(self.friendly_controller)
                ):
                    self.player_names[pid] = ent.name
        elif tag == "BACON_DUO_TEAMMATE_PLAYER_ID":
            if ent.tag("PLAYER_ID") == self.friendly_controller and str(value).isdigit():
                self.teammate_id = int(value)
        elif tag == "TURN" and value.isdigit():
            v = int(value)
            if not self._first_turn_seen:
                self._first_turn_seen = True
                if v > 1 and self._carryover:
                    # Reconnect into an ongoing game: merge the stashed data
                    # (player ids are stable across the reconnect).
                    carry = self._carryover
                    for pid, snaps in carry["history"].items():
                        self.history[pid] = snaps + self.history.get(pid, [])
                    for pid, snap in carry["snapshots"].items():
                        self.snapshots.setdefault(pid, snap)
                    for pid, nm in carry["names"].items():
                        self.player_names.setdefault(pid, nm)
                    self.hp_track = carry["hp_track"] + self.hp_track
                self._carryover = None
            self.turn = v
        elif tag == "STEP":
            self._on_step(value)
        elif tag == "BACON_GLOBAL_ANOMALY_DBID" and str(value).isdigit():
            self.anomaly_dbf = int(value)
        elif tag == "STATE" and value == "COMPLETE":
            self.game_over = True
            # The final combat has no following shopping turn; settle it now.
            self._resolve_combat_result()

    @staticmethod
    def _intval(value: str):
        return int(value) if value.lstrip("-").isdigit() else value

    # ------------------------------------------------------------ phase logic

    def _on_step(self, step: str):
        if not self.is_battlegrounds:
            return
        if step == "MAIN_READY":
            self._resolve_combat_result()
            self._pending_combat_opponent = 0
            self._pending_swap = 0
            self._pending_combat_friendly = 0
            self._pending_friendly_swap = 0
            self._in_combat = False
            self._cycle_started = False
        elif step == "MAIN_START":
            self._cycle_started = True
        if step == "MAIN_START" and self._pending_combat_opponent:
            # Warband setup (inside the BaconShop8PlayerEnchant trigger block
            # after MAIN_START_TRIGGERS) is usually complete here; attacks
            # have not begun. If a board is still empty (placement can land
            # a moment later), defer to the first attack block instead.
            self._in_combat = True
            self._last_combat_pid = self._pending_combat_opponent
            self._combat_snaps = []
            self._combat_pre = (
                self._pid_hp(self.friendly_controller),
                self._pid_hp(self._last_combat_pid),
            )
            if self._enemy_board():
                self._take_snapshot(self._pending_combat_opponent)
            else:
                self._pending_swap = self._pending_combat_opponent
            self._pending_combat_opponent = 0

            fpid = self._pending_combat_friendly
            self._last_friendly_pid = fpid
            # Snapshot our own side too (whoever leads it — us or, in duos,
            # our teammate); this builds the per-combat record of our own
            # boards used by the stats history.
            if fpid:
                if self._board(self.friendly_controller):
                    self._take_snapshot(fpid, friendly_side=True)
                else:
                    self._pending_friendly_swap = fpid
            self._pending_combat_friendly = 0
        elif step == "MAIN_END":
            # Combat over. If a board swapped in but never got to attack,
            # capture whatever it looks like now.
            self._flush_pending_swaps()
            self._in_combat = False

    def _board(self, controller):
        out = []
        for ent in self.entities.values():
            if (
                ent.tag("ZONE") == "PLAY"
                and ent.tag("CONTROLLER") == controller
                and ent.tag("CARDTYPE") == "MINION"
            ):
                out.append(ent)
        out.sort(key=lambda e: e.tag("ZONE_POSITION", 0))
        return out

    def _enemy_board(self):
        return self._board(self.enemy_controller)

    def friendly_board(self):
        """Our own current board as Minions (live during shopping)."""
        return [self._minion(e) for e in self._board(self.friendly_controller)]

    def friendly_tech_level(self) -> int:
        """Our current tavern tier, tracked live on friendly PLAY entities."""
        best = 0
        for ent in self.entities.values():
            if ent.tag("ZONE") == "PLAY" and ent.tag("CONTROLLER") == self.friendly_controller:
                lvl = ent.tag("PLAYER_TECH_LEVEL", 0)
                if isinstance(lvl, int) and lvl > best:
                    best = lvl
        return best

    def _flush_pending_swaps(self):
        if self._pending_swap:
            self._take_snapshot(self._pending_swap)
            self._last_combat_pid = self._pending_swap
            self._pending_swap = 0
        if self._pending_friendly_swap:
            self._take_snapshot(self._pending_friendly_swap, friendly_side=True)
            self._last_friendly_pid = self._pending_friendly_swap
            self._pending_friendly_swap = 0

    def _combat_hero(self, pid: int, controller: int) -> Entity | None:
        """The copy of a fighter's hero placed in PLAY for this combat."""
        for ent in self.entities.values():
            if (
                ent.tag("ZONE") == "PLAY"
                and ent.tag("CONTROLLER") == controller
                and ent.tag("CARDTYPE") == "HERO"
                and ent.tag("PLAYER_ID") == pid
            ):
                return ent
        return None

    def _resolve_combat_result(self):
        """After a combat, attribute win/loss/tie to its snapshots based on
        how the teams' health changed (synced before the next shopping turn)."""
        if not self._combat_pre or not self._combat_snaps:
            self._combat_pre = None
            return
        (own0, enemy0) = self._combat_pre
        own_delta = own0 - self._pid_hp(self.friendly_controller)
        enemy_delta = enemy0 - self._pid_hp(self._last_combat_pid)
        if own_delta > 0:
            result, dmg = "loss", own_delta
        elif enemy_delta > 0:
            result, dmg = "win", enemy_delta
        else:
            result, dmg = "tie", 0
        for snap in self._combat_snaps:
            snap.result = result
            snap.result_dmg = dmg
        hp_now = (self.turn, self._pid_hp(self.friendly_controller))
        if self.hp_track and self.hp_track[-1][0] == self.turn:
            self.hp_track[-1] = hp_now  # same combat resolved twice: keep newest
        else:
            self.hp_track.append(hp_now)
        self._combat_pre = None
        self._combat_snaps = []

    def _pid_hp(self, pid: int) -> int:
        st = self.player_status().get(pid)
        return (st["hp"] + st["armor"]) if st else 0

    def player_status(self):
        """Live per-player hp/armor/placement from the leaderboard-synced
        hero entities (SETASIDE on the enemy seat) plus our own live hero."""
        best: dict[int, tuple] = {}
        for ent in self.entities.values():
            pid = ent.tag("PLAYER_ID")
            if not pid or ent.tag("CARDTYPE") != "HERO":
                continue
            zone, ctrl = ent.tag("ZONE"), ent.tag("CONTROLLER")
            if (
                ctrl == self.friendly_controller
                and pid == self.friendly_controller
                and not ent.tag("COPIED_FROM_ENTITY_ID")
            ):
                # Our own real hero — still authoritative after it leaves
                # PLAY (death moves it to GRAVEYARD but the tags remain).
                # Own seat only: other players' copies also land on our
                # controller with stale default stats.
                rank = 3 if zone == "PLAY" else 2
            elif zone == "SETASIDE" and ctrl == self.enemy_controller:
                rank = 1  # leaderboard sync entity
            else:
                continue
            if pid not in best or rank > best[pid][0]:
                best[pid] = (
                    rank,
                    (ent.tag("HEALTH", 0) or 0) - (ent.tag("DAMAGE", 0) or 0),
                    ent.tag("ARMOR", 0) or 0,
                    ent.tag("PLAYER_LEADERBOARD_PLACE", 0) or 0,
                )
        return {
            pid: {"hp": v[1], "armor": v[2], "place": v[3]} for pid, v in best.items()
        }

    def _side_extras(self, controller):
        trinkets = []
        hero_power = ""
        for ent in self.entities.values():
            if ent.tag("ZONE") != "PLAY" or ent.tag("CONTROLLER") != controller:
                continue
            ctype = ent.tag("CARDTYPE")
            if ctype == "BATTLEGROUND_TRINKET" and not ent.card_id.startswith(
                "BG30_Trinket_"
            ):
                trinkets.append(ent.card_id)
            elif ctype == "HERO_POWER" and ent.card_id:
                hero_power = ent.card_id
        return trinkets, hero_power

    def _take_snapshot(self, opponent_id: int, friendly_side: bool = False):
        controller = self.friendly_controller if friendly_side else self.enemy_controller
        board = self._board(controller)
        if not board:
            return  # nothing placed — don't overwrite a good snapshot
        hero = self._combat_hero(opponent_id, controller)
        lobby_hero = self.lobby_heroes().get(opponent_id)
        hero_name = (lobby_hero.name if lobby_hero else "") or (hero.name if hero else "")
        trinkets, hero_power = self._side_extras(controller)
        status = self.player_status().get(opponent_id, {})
        snap = Snapshot(
            player_id=opponent_id,
            hero_card_id=(hero.card_id if hero else "")
            or (lobby_hero.card_id if lobby_hero else ""),
            hero_name=hero_name,
            round_num=self.turn,
            tech_level=hero.tag("PLAYER_TECH_LEVEL", 0) if hero else 0,
            health=status.get("hp", 0),
            armor=status.get("armor", 0),
            trinkets=trinkets,
            hero_power=hero_power,
            buddy_dbf=(hero.tag("BACON_COMPANION_ID", 0) or 0) if hero else 0,
            minions=[self._minion(e) for e in board],
        )
        self.snapshots[opponent_id] = snap
        lst = self.history.setdefault(opponent_id, [])
        if lst and lst[-1].round_num == snap.round_num:
            lst[-1] = snap  # re-capture of the same combat: keep the newest
        else:
            lst.append(snap)
        self._combat_snaps.append(snap)
        if self.debug:
            print(f"-- snapshot: turn={self.turn} round={snap.round_num} "
                  f"opp={opponent_id} hero={snap.hero_name} ({snap.hero_card_id})")
            for m in snap.minions:
                print(f"     {m.label()}")
        if self.on_snapshot:
            self.on_snapshot(snap)

    @staticmethod
    def _minion(ent: Entity) -> Minion:
        keywords = [label for tag, label in KEYWORD_TAGS.items() if ent.tag(tag)]
        return Minion(
            card_id=ent.card_id,
            name=ent.name,
            attack=ent.tag("ATK", 0) or 0,
            health=max((ent.tag("HEALTH", 0) or 0) - (ent.tag("DAMAGE", 0) or 0), 0),
            golden=bool(ent.tag("PREMIUM")),
            keywords=keywords,
        )

    # ------------------------------------------------------------------ query

    def lobby_heroes(self):
        """All lobby hero entities (one per player), by lobby player id."""
        out = {}
        for ent in self.entities.values():
            pid = ent.tag("PLAYER_ID")
            if (
                pid
                and ent.tag("CARDTYPE") == "HERO"
                and ent.card_id
                and ent.card_id not in INNKEEPER_CARDS
                and not ent.tag("COPIED_FROM_ENTITY_ID")  # skip combat copies
            ):
                # Prefer an entity we have a display name for.
                if pid not in out or (ent.name and not out[pid].name):
                    out[pid] = ent
        return out

    def duo_teams(self):
        """Duos: lobby player id -> team id (empty dict in solo games)."""
        out = {}
        for ent in self.entities.values():
            team = ent.tag("BACON_DUO_TEAM_ID")
            pid = ent.tag("PLAYER_ID")
            if team and pid:
                out[pid] = team
        return out


def replay(path: str, debug: bool = True):
    game = BgGame(debug=debug)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            game.feed_line(line.rstrip("\n"))
    return game


if __name__ == "__main__":
    import sys

    game = replay(sys.argv[1], debug=True)
    print(f"\nBattlegrounds: {game.is_battlegrounds}  "
          f"controllers: friendly={game.friendly_controller} enemy={game.enemy_controller}")
    print(f"player names: {game.player_names}")
    print("\nlobby heroes:")
    for pid, ent in sorted(game.lobby_heroes().items()):
        place = ent.tag("PLAYER_LEADERBOARD_PLACE")
        print(f"  player {pid}: {ent.name or ent.card_id}  place={place} "
              f"tier={ent.tag('PLAYER_TECH_LEVEL')}")
    print("\nfinal snapshots:")
    for pid, snap in sorted(game.snapshots.items()):
        print(f"  player {pid} — {snap.hero_name} (round {snap.round_num}):")
        for m in snap.minions:
            print(f"      {m.label()}")
