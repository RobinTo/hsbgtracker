# BG Tracker

Lightweight Hearthstone Battlegrounds companion window. Shows each lobby
opponent's board **as it was the last time you fought them** — hover a hero
row to see it, click to pin it.

No install, no Overwolf, no memory reading: it tails Hearthstone's own
`Power.log` (the same file HDT reads).

## Run

```
pythonw app.py
```

(`python app.py` if you want a console for errors.)

## Requirements

- Python 3.10+ (stdlib only — tkinter, no packages to install)
- Hearthstone logging enabled: `%LOCALAPPDATA%\Blizzard\Hearthstone\log.config`
  must contain a `[Power]` section with `LogLevel=1`, `FilePrinting=true`,
  `Verbose=true` (an HDT install leaves this in place)
- The log size cap lifted: `client.config` next to `Hearthstone.exe`
  containing:

  ```
  [Log]
  FileSizeLimit.Int=-1
  ```

  Without this, Hearthstone stops writing Power.log after 10MB (roughly one
  BG game) and every log-based tracker goes blind until the game restarts.
  HDT writes this exact file automatically; here it's set up manually.

## What it shows

- One row per lobby hero; `⚔` marks your next opponent
- Tavern tier per player: yours live, others as of your last fight with them
- Live HP + armor per player (leaderboard-synced), eliminated players show
  their placement; a post-game status line shows where you finished
- Dominant tribe per board ("Undead", "Mech", "Mixed") on each row
- Detail pane: fight result (won/lost by how much), hero power, trinkets,
  and a clickable round history of every board you've seen from that player
- Duos: rows are grouped by team (yours first, teammate labeled), and both
  enemy fighters of a combat are captured — the lead fighter's board at
  combat start, the partner's board when it swaps in mid-combat. Your
  teammate's board is captured the same way whenever they fight. Each team
  group shows a Σ attack/health caption over last-seen boards for relative
  strength (your own entry is your live board)
- Hover a row → last-seen board: minions with attack/health, golden (★),
  and keywords (Taunt, Divine Shield, Poisonous, Reborn, ...)
- Round the board was seen on, the player's battletag when the log reveals
  it, and their tavern tier at that time
- Click a row to pin it (click again to unpin); "on top" checkbox toggles
  always-on-top

Boards are captured at the *start* of each combat, before any attacks
resolve — i.e. the board exactly as the opponent built it.

## Recap, history, stats

- When a game ends the pane shows a recap (HP graph + placements); the
  "📊 recap" button or Escape brings it back after hovering elsewhere
- Every finished game is appended to `games_history.jsonl` (including your
  own board from every combat)
- `python stats.py` reports average placement, placement by final-board
  tribe, and the minions most common in your winning vs losing fights

## Keyboard

Up/Down cycle players · Left/Right cycle a player's recorded rounds ·
Escape back to default view / recap

## Tests

`python tests/test_replay.py` replays stored logs in `tests/data/`
(machine-local, not committed) and asserts known-good snapshots, results,
and placements.

## Files

- `app.py` — tkinter UI + Power.log tailer (auto-detects the install from
  the registry)
- `parser.py` — Power.log → game state; run standalone to replay a log:
  `python parser.py <path-to-Power.log>`
- `cards.py` — card-ID → name lookup; downloads HearthstoneJSON once into
  `cards_cache.json`, falls back to names learned from the log itself

## Limitations

- A board is only known after you've fought that player (that's inherent —
  the log only reveals what your client has seen)
- Start-of-shopping-phase changes (e.g. what they bought since) are
  invisible until you fight them again
- Duos: if a combat ends before the enemy partner's board gets to attack,
  their snapshot reflects the board state at combat end rather than as-built
- Regular Hearthstone / other modes are ignored by design
- If Hearthstone runs in exclusive fullscreen the always-on-top window can't
  overlay it; use Windowed / Borderless, or put the tracker on a second
  monitor
