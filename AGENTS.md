# sleeper-gm, rules for agents and contributors

An automated manager for Sleeper NBA leagues in Lock-In mode. The objective is the probability of
winning each weekly head-to-head matchup (and the season), not raw points.

## Lock-In rules the code depends on (Sleeper help center, read live, never assume)
- Weeks run Monday-Sunday. Each starter counts exactly ONE game per week.
- After a starter's game ends it may be locked (`starters_games[player_id] = game_id`) until that player's
  NEXT game tips. Locks are final and pin the player to his slot.
- An unlocked starter scores his team's FINAL game of the week (0 if he sits). There is no fallback to an
  earlier game.
- A game is lockable only if the player was in a starting slot at tip-off and was never benched since.
  Moving between starting slots keeps eligibility. A bench game never counts.
- A player added mid-week only scores games he plays in your lineup after the add. A postponed game is not
  a game: the previous one stays lockable until the rescheduled tip.
- Slots: PG, SG, G (PG/SG), SF, PF, F (SF/PF), C, UTIL. A healthy player left on IR freezes the lineup in
  Sleeper's apps until moved off.
- League specifics (slots, bench size, IR slots and eligible designations, waiver type, scoring) come from
  the league object every run. Only leagues with `settings.game_mode == 1` (Lock-In) are managed.

## API facts
- REST `api.sleeper.app/v1` and `api.sleeper.com/{stats,projections}/nba/<season>/<week>` are public and
  Cloudflare-cached for minutes. Weekly stats/projections are ONE ROW PER GAME. Send a User-Agent.
- GraphQL `sleeper.com/graphql` (undocumented): `scores` (tip times) needs no token; `matchup_legs`,
  `league_rosters`, `league_transactions` need it. Never confirm a write from REST.
- Lineup and locks are one `update_matchup_leg(round=week, leg=week, starters=[positional ids], starters_games=<JSON map>)`;
  `starters_games` must be the FULL map. `roster_update_starters` is the WRONG mutation (it scores nothing).
- Claims use `submit_waiver_claim`, `update_waiver_claim`, and `cancel_waiver_claim` only.
  Arguments and web strings are documented in `docs/sleeper-api-notes.md`. Rolling submits send
  no settings; FAAB submits send only `waiver_bid: 0`. Never send metadata or write server-assigned
  `seq` or `priority`. The bot appends claims using server order and never reorders anything;
  the owner can reorder claims in the app. Never call `Writer.update_claim` from reconciliation.
- Pending claims are authenticated `league_transactions(status:"pending", roster_id:...)` reads.
  Bot ids and intents are local league state. Save a fresh pending-id baseline before submission;
  recover by response id or a unique new pending add/drop identity, never an existing owner row.
  An unresolved intent blocks further claims. Only bot claims may be cancelled; owner claims are
  modeled first. Up to `max_claims` (default 3) bot claims may be pending. Re-plan on full
  runs, keeping eligible incumbents unless they fall below their required edge or a replacement
  beats their current score by at least 0.01 P(win). Never change claims within 15 minutes of
  estimated processing, and confirm in pending reads. At most `max_claim_writes_per_day` (default 6)
  claim mutation attempts per league per ET day, including errors, counted from the writes log.
- Estimate processing from median `status_updated` ET time on processed claims, default 03:10 ET.
  Player waiver eligibility remains unknown until tried; a processing run does not prove clearance.
  Three free-agent waiver refusals switch a league to claims until a successful free-agent add.
  REST roster `settings.waiver_position` was verified read-only. Never use commissioner transaction tools.
  Trades are never sent.
- Writes live only in `sleeper_gm/api.py` `Writer`, which refuses unless live. Never retry a write.

## Working here
- `pip install -e ".[dev,mcp,browser]"`, then `pytest -q`. Tests are offline.
- A test whose fixture does not vary the thing the behaviour turns on is not a test: parameterize it.
- Backtests use only data strictly before the predicted game.
- Nothing personal in the repo: config, token, logs and caches live in the XDG dirs (`sleeper_gm/config.py`).
  Never print or log the token.
- No em dashes in code, comments, docs or messages.
