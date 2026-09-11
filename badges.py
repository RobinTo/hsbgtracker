"""Career badges for heroes, final tribes and trinkets.

A badge is a short verdict ("Wins early", "Bottom feeder") earned when a
subject's record over your games clears a threshold with enough sample. The
same definitions feed the tracker's hero-pick panel and the stats page, so
both always agree.

    from badges import compute_badges
    b = compute_badges(load_games(), cards)
    b["heroes"]["BG20_HERO_202"]   -> [{"id", "label", "tone", "detail"}, ...]
    b["tribes"]["Dragon"], b["trinkets"]["Wax Lance"]

Thresholds are deliberately blunt; the point is a fun, glanceable verdict,
and the ``detail`` string carries the exact numbers for a tooltip.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from types import SimpleNamespace

from stats import own_snaps

MIN_GAMES = 5        # subject needs this many placed games for any badge
MIN_FIGHTS = 15      # ...and this many fights for a fight-rate badge
EARLY_ROUNDS = (1, 4)
LATE_ROUND = 9
STREAK = 5           # games in a hot/cold streak

_SKIN_RE = re.compile(r"_SKIN_[A-Z0-9]+$")


def _base_hero(card_id: str) -> str:
    return _SKIN_RE.sub("", card_id or "")


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _derive(g: dict, cards) -> SimpleNamespace | None:
    """Flatten one history record into what the badge rules look at."""
    place = g.get("place") or 0
    if not place:
        return None
    snaps = own_snaps(g)
    hero = ""
    for s in snaps:
        if s.get("hero_card_id"):
            hero = _base_hero(s["hero_card_id"])
            break
    tribe = ""
    if snaps:
        tribe = cards.tribe_label(
            [SimpleNamespace(card_id=m["card_id"]) for m in snaps[-1]["minions"]]
        ) or ""
    trinkets: list[str] = []
    for s in snaps:
        for t in s.get("trinkets") or []:
            if t not in trinkets:
                trinkets.append(t)
    tier_ups = g.get("tierUps") or []
    if not tier_ups:
        last = 1
        for s in snaps:
            lvl = s.get("tech_level", 0)
            if lvl > last:
                tier_ups.append([s["round_num"], lvl])
                last = lvl
    tier_round = {lvl: r for r, lvl in tier_ups}
    hp_track = g.get("hp_track") or []
    seats = 4 if g.get("mode") == "duos" else 8
    return SimpleNamespace(
        ts=g.get("ts", ""),
        place=place,
        seats=seats,
        top=place <= seats // 2,
        first=place == 1,
        hero=hero,
        tribe=tribe,
        trinkets=trinkets,
        fights=[(s["round_num"], s.get("result")) for s in snaps
                if s.get("result") in ("win", "loss")],
        tier_round=tier_round,
        hp_track=hp_track,
        econ=g.get("econ"),
        rounds=hp_track[-1][0] if hp_track else (snaps[-1]["round_num"] if snaps else 0),
    )


def _hp_at(rec, round_num: int):
    """Health + armor at a round (last recorded value at or before it), or
    None if the game ended earlier."""
    if not rec.hp_track or rec.hp_track[-1][0] < round_num:
        return None
    val = None
    for r, hp in rec.hp_track:
        if r > round_num:
            break
        val = hp
    return val


class _Baseline:
    """Your overall numbers, so 'fast' and 'roller' mean relative to you."""

    def __init__(self, recs):
        self.t5_round = _mean(r.tier_round[5] for r in recs if 5 in r.tier_round)
        self.rolls_per_round = _mean(
            r.econ["rolls"] / r.rounds for r in recs if r.econ and r.rounds)
        hp8 = [v for v in (_hp_at(r, 8) for r in recs) if v is not None]
        self.hp8 = _mean(hp8)
        self.t6_rate = _pct(sum(1 for r in recs if 6 in r.tier_round), len(recs))
        self.rounds = _mean(r.rounds for r in recs if r.rounds)


def _badges_for(recs, base: _Baseline, subject_kind: str, cards) -> list[dict]:
    """Badge list for one subject's games (already filtered to it)."""
    out: list[dict] = []
    n = len(recs)
    if n < MIN_GAMES:
        return out

    def add(bid, label, tone, detail):
        out.append({"id": bid, "label": label, "tone": tone, "detail": detail})

    # ---------------------------------------------------------- placement
    tops = sum(1 for r in recs if r.top)
    firsts = sum(1 for r in recs if r.first)
    if _pct(firsts, n) >= 40:
        add("first-magnet", "First-place magnet", "good",
            f"{firsts} of {n} games won ({_pct(firsts, n):.0f}%)")
    elif _pct(tops, n) >= 75:
        add("top-machine", "Top-half machine", "good",
            f"{tops} of {n} games in the top half ({_pct(tops, n):.0f}%)")
    if _pct(n - tops, n) >= 60:
        add("bottom-feeder", "Bottom feeder", "bad",
            f"{n - tops} of {n} games in the bottom half ({_pct(n - tops, n):.0f}%)")

    # ------------------------------------------------------------- fights
    def fight_rate(lo, hi):
        w = t = 0
        for r in recs:
            for rnd, res in r.fights:
                if lo <= rnd <= hi:
                    t += 1
                    w += res == "win"
        return w, t

    w, t = fight_rate(*EARLY_ROUNDS)
    if t >= MIN_FIGHTS:
        p = _pct(w, t)
        if p >= 65:
            add("wins-early", "Wins early", "good",
                f"{p:.0f}% of round {EARLY_ROUNDS[0]}–{EARLY_ROUNDS[1]} fights won ({w}/{t})")
        elif p <= 40:
            add("loses-early", "Loses early", "bad",
                f"{p:.0f}% of round {EARLY_ROUNDS[0]}–{EARLY_ROUNDS[1]} fights won ({w}/{t})")
    w, t = fight_rate(LATE_ROUND, 99)
    if t >= MIN_FIGHTS:
        p = _pct(w, t)
        if p >= 65:
            add("closes-out", "Closes out", "good",
                f"{p:.0f}% of round {LATE_ROUND}+ fights won ({w}/{t})")
        elif p <= 40:
            add("fades-late", "Fades late", "bad",
                f"{p:.0f}% of round {LATE_ROUND}+ fights won ({w}/{t})")

    # ----------------------------------------------------------- tempo
    t5 = [r.tier_round[5] for r in recs if 5 in r.tier_round]
    if len(t5) >= MIN_GAMES and base.t5_round:
        avg = _mean(t5)
        if avg <= base.t5_round - 1.0:
            add("fast-lvl", "Fast leveller", "neutral",
                f"tier 5 by round {avg:.1f} on average (you: {base.t5_round:.1f})")
        elif avg >= base.t5_round + 1.0:
            add("slow-lvl", "Slow leveller", "neutral",
                f"tier 5 by round {avg:.1f} on average (you: {base.t5_round:.1f})")
    t6 = sum(1 for r in recs if 6 in r.tier_round)
    if _pct(t6, n) >= base.t6_rate + 15:
        add("reaches-t6", "Reaches T6", "neutral",
            f"tier 6 in {t6} of {n} games ({_pct(t6, n):.0f}%, you: {base.t6_rate:.0f}%)")
    elif _pct(t6, n) <= base.t6_rate - 20:
        add("stays-low", "Stays low", "neutral",
            f"tier 6 in only {t6} of {n} games ({_pct(t6, n):.0f}%, you: {base.t6_rate:.0f}%)")

    # ----------------------------------------------------------- economy
    rpr = [r.econ["rolls"] / r.rounds for r in recs if r.econ and r.rounds]
    if len(rpr) >= MIN_GAMES and base.rolls_per_round:
        avg = _mean(rpr)
        if avg >= base.rolls_per_round * 1.25:
            add("roller", "Roller", "neutral",
                f"{avg:.2f} rolls per round (you: {base.rolls_per_round:.2f})")
        elif avg <= base.rolls_per_round * 0.75:
            add("thrifty", "Thrifty", "neutral",
                f"{avg:.2f} rolls per round (you: {base.rolls_per_round:.2f})")

    # ------------------------------------------------------------ health
    hp8 = [v for v in (_hp_at(r, 8) for r in recs) if v is not None]
    if len(hp8) >= MIN_GAMES and base.hp8:
        avg = _mean(hp8)
        if avg >= base.hp8 + 8:
            add("survivor", "Survivor", "good",
                f"{avg:.0f} health at round 8 on average (you: {base.hp8:.0f})")
        elif avg <= base.hp8 - 8:
            add("fragile", "Fragile", "bad",
                f"{avg:.0f} health at round 8 on average (you: {base.hp8:.0f})")
    comebacks = sum(
        1 for r in recs if r.first
        and any(rnd < 8 and hp < 15 for rnd, hp in r.hp_track))
    if comebacks >= 2:
        add("comeback", "Comeback kid", "good",
            f"{comebacks} wins after dropping under 15 health before round 8")

    # ------------------------------------------------------------- tribe
    if subject_kind == "hero":
        tribes = Counter(r.tribe for r in recs if r.tribe and r.tribe != "Mixed")
        if tribes:
            tribe, cnt = tribes.most_common(1)[0]
            if _pct(cnt, n) >= 50:
                add("loves-tribe", f"Loves {tribe}", "neutral",
                    f"ended on {tribe} in {cnt} of {n} games ({_pct(cnt, n):.0f}%)")

    # ------------------------------------------------------------ streaks
    last = recs[-STREAK:]
    if len(last) == STREAK:
        if all(r.top for r in last):
            add("hot", "Hot streak", "good", f"last {STREAK} games all top half")
        elif all(not r.top for r in last):
            add("cold", "Cold streak", "bad", f"last {STREAK} games all bottom half")
    return out


def compute_badges(games: list[dict], cards) -> dict:
    """Badges for every hero (by base card), final tribe and trinket (by name)
    with enough games. Games are taken in stored (chronological) order."""
    recs = [r for r in (_derive(g, cards) for g in games) if r is not None]
    base = _Baseline(recs)
    by_hero: dict[str, list] = defaultdict(list)
    by_tribe: dict[str, list] = defaultdict(list)
    by_trinket: dict[str, list] = defaultdict(list)
    for r in recs:
        if r.hero:
            by_hero[r.hero].append(r)
        if r.tribe:
            by_tribe[r.tribe].append(r)
        for t in r.trinkets:
            by_trinket[cards.name(t)].append(r)

    def build(groups, kind):
        out = {}
        for key, rs in groups.items():
            b = _badges_for(rs, base, kind, cards)
            if b:
                out[key] = b
        return out

    return {
        "heroes": build(by_hero, "hero"),
        "tribes": build(by_tribe, "tribe"),
        "trinkets": build(by_trinket, "trinket"),
    }


# ------------------------------------------------------------ one game
def _opponent_by_round(rec: dict) -> dict[int, str]:
    """round -> lobby pid we fought, from the opponent snapshots the tracker
    takes each combat (own and teammate snapshots excluded)."""
    skip = {str(rec.get("own_pid")), str(rec.get("teammate"))}
    out: dict[int, str] = {}
    for pid, snaps in (rec.get("history") or {}).items():
        if pid in skip:
            continue
        for s in snaps:
            out.setdefault(s["round_num"], pid)
    return out


def game_summary(rec: dict, prior_games: list[dict], cards) -> dict:
    """Badges and fact lines for one finished game, judged against your
    earlier games. ``rec`` is a history record (as persisted, or freshly
    built from the live view); ``prior_games`` is the history, which may
    already contain this game (matched by ``sig`` and excluded).

    Returns {"badges": [{"label", "tone", "detail"}], "facts": [str]}."""
    import json

    rec = json.loads(json.dumps(rec))  # normalise int/str keys like the file
    me = _derive(rec, cards)
    if me is None:
        return {"badges": [], "facts": []}
    prior = [r for r in (_derive(g, cards) for g in prior_games
                         if g.get("sig") != rec.get("sig")) if r is not None]
    base = _Baseline(prior) if len(prior) >= MIN_GAMES else None
    badges: list[dict] = []
    facts: list[str] = []

    def add(label, tone, detail):
        badges.append({"id": label.lower().replace(" ", "-"), "label": label,
                       "tone": tone, "detail": detail})

    snaps = own_snaps(rec)
    results = [(s["round_num"], s.get("result"), s.get("result_dmg", 0)) for s in snaps
               if s.get("result") in ("win", "loss", "tie")]
    wins = [(r, d) for r, res, d in results if res == "win"]
    losses = [(r, d) for r, res, d in results if res == "loss"]
    ties = sum(1 for _r, res, _d in results if res == "tie")
    n_fights = len(results)

    # ---------------------------------------------------------- fights
    if n_fights:
        line = f"{n_fights} fights: {len(wins)}W {len(losses)}L"
        if ties:
            line += f" {ties}T"
        dealt = sum(d for _r, d in wins)
        taken = sum(d for _r, d in losses)
        line += f" · dealt {dealt} · took {taken}"
        facts.append(line)
    if n_fights >= 5 and not losses:
        if me.first:
            add("Flawless", "good", f"won the game without losing a fight ({len(wins)} wins)")
        else:
            add("Undefeated", "good", f"never lost a fight ({len(wins)} wins)")
    early = [(res) for r, res, _d in results if r <= EARLY_ROUNDS[1] and res != "tie"]
    late = [(res) for r, res, _d in results if r >= LATE_ROUND and res != "tie"]
    if len(early) >= 3 and all(x == "win" for x in early):
        add("Early bully", "good", f"won all {len(early)} fights in rounds 1–{EARLY_ROUNDS[1]}")
    elif len(early) >= 3 and all(x == "loss" for x in early):
        add("Rough start", "bad", f"lost all {len(early)} fights in rounds 1–{EARLY_ROUNDS[1]}")
    if (len(early) >= 3 and len(late) >= 3
            and _pct(early.count("win"), len(early)) <= 40
            and _pct(late.count("win"), len(late)) >= 67):
        add("Late bloomer", "good",
            f"{early.count('win')}/{len(early)} early fights won, then {late.count('win')}/{len(late)} late")
    best_run = run = 0
    worst_run = lrun = 0
    for _r, res, _d in results:
        run = run + 1 if res == "win" else 0
        lrun = lrun + 1 if res == "loss" else 0
        best_run, worst_run = max(best_run, run), max(worst_run, lrun)
    if best_run >= 5:
        add("On a roll", "good", f"{best_run} fight wins in a row")
    if worst_run >= 4:
        add("Free fall", "bad", f"{worst_run} fight losses in a row")
    if wins and max(d for _r, d in wins) >= 20:
        r, d = max(wins, key=lambda x: x[1])
        add("Knockout punch", "good", f"hit for {d} in round {r}")
    if losses and max(d for _r, d in losses) >= 20:
        r, d = max(losses, key=lambda x: x[1])
        add("Took a beating", "bad", f"took {d} in round {r}")

    # ---------------------------------------------------------- health
    if me.hp_track:
        lows = [hp for r, hp in me.hp_track if r < 8]
        if lows and min(lows) < 15 and me.top:
            add("Comeback", "good", f"down to {min(lows)} health before round 8, finished #{me.place}")
        if me.rounds >= 8 and min(hp for _r, hp in me.hp_track) >= 25:
            add("Iron wall", "good", f"never dropped below {min(hp for _r, hp in me.hp_track)} health")

    # ----------------------------------------------------------- tempo
    if base:
        t5 = me.tier_round.get(5)
        if t5 and base.t5_round:
            if t5 <= base.t5_round - 1.5:
                add("Speedrun", "neutral", f"tier 5 by round {t5} (you average r{base.t5_round:.1f})")
            elif t5 >= base.t5_round + 2:
                add("Slow cook", "neutral", f"tier 5 only by round {t5} (you average r{base.t5_round:.1f})")
        if me.econ and me.rounds and base.rolls_per_round:
            rpr = me.econ["rolls"] / me.rounds
            if rpr >= base.rolls_per_round * 1.5:
                add("Roll happy", "neutral",
                    f"{rpr:.1f} rolls per round (you average {base.rolls_per_round:.1f})")
            elif rpr <= base.rolls_per_round * 0.5:
                add("Thrifty", "neutral",
                    f"{rpr:.1f} rolls per round (you average {base.rolls_per_round:.1f})")
        if me.rounds and base.rounds:
            if me.rounds >= base.rounds + 3:
                add("Marathon", "neutral", f"{me.rounds} rounds (you average {base.rounds:.1f})")
            elif me.rounds <= 7:
                add("Blitz", "neutral", f"over by round {me.rounds}")
        econ_line = []
        if me.econ:
            econ_line.append(f"rolls {me.econ['rolls']} · buys {me.econ['buys']} · sells {me.econ['sells']}")
        if 6 in me.tier_round:
            econ_line.append(f"tier 6 by r{me.tier_round[6]}")
        elif me.tier_round:
            top_tier = max(me.tier_round)
            econ_line.append(f"tier {top_tier} by r{me.tier_round[top_tier]}")
        if econ_line:
            facts.append(" · ".join(econ_line))

    # ------------------------------------------------------- opponents
    opp = _opponent_by_round(rec)
    statuses = rec.get("statuses") or {}
    heroes = rec.get("heroes") or {}
    hero_name = lambda pid: heroes.get(pid) or f"player {pid}"
    if not me.first:
        winners = {pid for pid, st in statuses.items() if st.get("place") == 1}
        # Early wins over the eventual winner are routine; a late one is a story.
        beat = [r for r, _d in wins if r >= 7 and opp.get(r) in winners]
        if beat:
            add("Giant slayer", "good",
                f"beat the eventual winner ({hero_name(opp[beat[-1]])}) in round {beat[-1]}")
    lost_to = Counter(opp[r] for r, _d in losses if r in opp)
    if lost_to:
        pid, cnt = lost_to.most_common(1)[0]
        if cnt >= 2:
            facts.append(f"Nemesis: {hero_name(pid)} beat you {cnt} times")

    # ---------------------------------------------------------- career
    # Placement averages only make sense within a mode (4th means different
    # things in solo and duos), so compare against same-mode games.
    prior_mode = [r for r in prior if r.seats == me.seats]
    same_hero = [r for r in prior_mode if r.hero == me.hero] if me.hero else []
    if me.hero:
        hero_label = cards.name(me.hero) if cards.known(me.hero) else hero_name(str(rec.get("own_pid")))
        mode_label = "duos" if me.seats == 4 else "solo"
        if not same_hero:
            facts.append(f"First {mode_label} game with {hero_label}")
        else:
            places = [r.place for r in same_hero] + [me.place]
            facts.append(f"{mode_label.capitalize()} game {len(places)} with {hero_label}"
                         f" · average #{_mean(places):.1f}")
            if len(same_hero) >= 3 and me.place < min(r.place for r in same_hero):
                add("Personal best", "good",
                    f"best finish with {hero_label} in {len(places)} games")
    if me.tribe and me.tribe != "Mixed":
        same_tribe = [r for r in prior_mode if r.tribe == me.tribe]
        if len(same_tribe) >= 3:
            facts.append(f"Ended on {me.tribe} · you average #{_mean(r.place for r in same_tribe):.1f}"
                         f" with {me.tribe} over {len(same_tribe)} {mode_label} games")
    streak = 1 if me.top else 0
    if me.top:
        for r in reversed(prior):
            if not r.top:
                break
            streak += 1
    best_prior = run = 0
    for r in prior:
        run = run + 1 if r.top else 0
        best_prior = max(best_prior, run)
    if streak >= 3 and streak > best_prior:
        add("New record", "good", f"{streak} top-half finishes in a row, your longest ever")
    elif streak >= 3:
        facts.append(f"{streak} top-half finishes in a row (best {best_prior})")

    return {"badges": badges, "facts": facts}


if __name__ == "__main__":
    from cards import CardDb
    from stats import load_games

    cards = CardDb()
    games = load_games()
    if games:
        print("=== last game")
        summary = game_summary(games[-1], games, cards)
        for b in summary["badges"]:
            print(f"  [{b['label']}] {b['detail']}")
        for f in summary["facts"]:
            print(f"  {f}")
    result = compute_badges(games, cards)
    for kind, subjects in result.items():
        print(f"=== {kind}")
        for key, bs in subjects.items():
            name = cards.name(key) if kind == "heroes" else key
            print(f"  {name}: " + ", ".join(b["label"] for b in bs))
