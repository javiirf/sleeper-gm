# Waiver claims: read-only evidence and policy

Verified 2026-10-29 using public GETs and GraphQL queries/introspection only. No
mutation was executed. Examples use fictional placeholders. These private APIs can change.

## Sources and method

- [GraphQL endpoint](https://sleeper.com/graphql): anonymous introspection, using
  snake_case `__schema { mutation_type ... query_type ... }` and
  `__type(name:"LeagueTransaction")`. Type references were followed through three
  `of_type` levels, not inferred from names.
- [Public league web shell](https://sleeper.com/leagues) referenced
  [this public JS bundle](https://sleepercdn.com/js/bundle-cf8c2e018e710f218a70cd18b154a1b4.js?vsn=d).
  GET the bundle and search for `t.claimWaiver=function`, `t.updateWaiver=function`,
  `t.cancelWaiver=function`, `_onClickSubmit`, `settings.priority`, and `waiver_bid`.
  The marketing homepage uses different Next.js bundles; the league shell contains
  the relevant fantasy actions.
- A public REST `/v1/league/<league>/rosters` read confirmed that roster
  `settings.waiver_position` exists and is an integer. No identifiers or roster
  contents from that read are retained here. Full cycles already read roster
  `settings` through authenticated `league_rosters`; the planner uses that fresher copy.
- An authenticated pending query selecting every field below was accepted. There
  were no pending rows at inspection time, so actual pending values were not sampled.
- Live [Lock-In help](https://support.sleeper.com/en/articles/6522833-lock-in-mode-details)
  confirms that acquisitions cannot score earlier games, locking requires starting at
  tip, and an unlocked player uses his team's final game without fallback.
- Live [basketball waiver help](https://support.sleeper.com/en/articles/4678615-how-do-faab-and-waivers-work)
  confirms $0 FAAB bids are allowed. It does not provide a model for an optimal paid bid.

## Exact introspected types

All three mutations return nullable `LeagueTransaction` (`OBJECT`, no wrapper).

| Mutation | Arguments and GraphQL types |
|---|---|
| `submit_waiver_claim` | `league_id: Snowflake!`; `k_adds`, `k_drops`, `k_settings`, `k_metadata`, `v_metadata`: `[String]`; `v_adds`, `v_drops`, `v_settings`: `[Int]` |
| `update_waiver_claim` | `leg: Int!`, `league_id: Snowflake!`, `transaction_id: Snowflake!`; `k_settings`, `k_metadata`, `v_metadata`: `[String]`; `v_settings`: `[Int]` |
| `cancel_waiver_claim` | `leg: Int!`, `league_id: Snowflake!`, `transaction_id: Snowflake!` |

For example, `[String]` was `kind: LIST, name: null, of_type: {kind: SCALAR,
name: String, of_type: null}`. Both the list and its elements are nullable. A required
identifier was `NON_NULL -> SCALAR Snowflake`; `leg` was `NON_NULL -> SCALAR Int`.
There is no `leg` or `roster_id` argument on submit. The roster is encoded in the
integer `v_adds`/`v_drops` lists, parallel to player ids in the key lists.

`LeagueTransaction` fields (all nullable):

| Type | Fields |
|---|---|
| `Snowflake` | `league_id`, `creator`, `transaction_id` |
| `String` | `status`, `type` |
| `Int` | `created`, `leg`, `status_updated` |
| `Map` | `metadata`, `settings`, `adds`, `drops`, `player_map` |
| `List` | `draft_picks`, `consenter_ids`, `roster_ids`, `waiver_budget` |

`Map` and `List` above are custom scalars, not GraphQL object/list selections.
Transaction timestamps are epoch milliseconds despite the schema's `Int` name.

`league_transactions` returns `[LeagueTransaction]`. Its arguments are
`league_id: Snowflake!`, `status: String`, `type: String`, `limit: Int`, `leg: Int`,
`roster_id: Int`. This accepted query includes the requested pending fields:

```graphql
{
  league_transactions(league_id:"LEAGUE", leg:2, status:"pending", roster_id:1) {
    transaction_id type status adds drops settings creator created leg roster_ids
    metadata status_updated
  }
}
```

The implementation omits optional `leg` when reading pending claims, so pending
claims from a previous week remain visible. Terminal history uses current/previous
legs plus any unreported tracked legs. Authenticated reads confirm writes, never REST.

## Exact web mutation strings

These are the bundle's assembled strings with whitespace normalized and interpolated
ids replaced with `LEAGUE`, `TRANSACTION`, and leg `2`. Submit has optional settings:
when no settings are provided, its `$k_settings`/`$v_settings` declarations and
`k_settings`/`v_settings` arguments are omitted. None of these strings was executed.

```graphql
mutation submit_waiver_claim($k_adds: [String], $v_adds: [Int], $k_drops: [String], $v_drops: [Int], $k_settings: [String], $v_settings: [Int]) {
  submit_waiver_claim(league_id: "LEAGUE", k_adds: $k_adds, v_adds: $v_adds, k_drops: $k_drops, v_drops: $v_drops, k_settings: $k_settings, v_settings: $v_settings) {
    adds consenter_ids created creator drops league_id leg metadata roster_ids settings status status_updated transaction_id type player_map
  }
}

mutation update_waiver_claim($k_settings: [String], $v_settings: [Int]) {
  update_waiver_claim(league_id: "LEAGUE", transaction_id: "TRANSACTION", leg: 2, k_settings: $k_settings, v_settings: $v_settings) {
    adds consenter_ids created creator drops league_id leg metadata roster_ids settings status status_updated transaction_id type player_map
  }
}

mutation cancel_waiver_claim {
  cancel_waiver_claim(league_id: "LEAGUE", leg: 2, transaction_id: "TRANSACTION") {
    adds consenter_ids created creator drops league_id leg metadata roster_ids settings status status_updated transaction_id type player_map
  }
}
```

Settings evidence from the same bundle:

- Submit's UI assigns `n.waiver_bid=+e.state.bid` only for FAAB, then calls
  `claimWaiver(leagueId, adds, drops, settings)`; the action converts the settings
  object into parallel key/value arrays.
- Reordering assigns `r.priority=e[a.transaction_id]`, where the index is zero based,
  then calls `updateWaiver(a.league_id,a.transaction_id,a.leg,r)`. It preserves other
  existing settings.
- Pending display sorts by `["settings.waiver_bid","settings.priority","transaction_id"]`
  in `["desc","asc","asc"]` order.
- Bid editing merges existing settings with `{waiver_bid:p}` before updating.
- `settings.seq` is server-assigned and is never written, including when passing through
  existing settings during a priority update. It is read for ordering and display only.
- Rolling submits omit settings arguments and variables entirely. FAAB submits send exactly
  `{"waiver_bid": 0}`. New claims follow existing claims by the server's assigned sequence.
- Only if at least one owner claim has an explicit `priority` (including 0), update the
  bot's own claims to `max(owner priority) + 1 + i`. The update passes the row's existing
  writable settings plus that priority, excluding read-only `seq`. Owner claims are never
  reordered. With no owner priority, no ordering update is sent. Read-back compares only
  values actually sent, never an invented expected sequence.
- Although the schema supports `k_metadata`/`v_metadata`, neither the web actions nor the
  bot send them. Transaction metadata is exposed through the public REST transactions
  endpoint. Sending a bot marker would leave a public fingerprint with no need for it.
- Ownership is entirely local. Immediately before each submit, read the pending list and
  durably save its transaction ids with the add/drop intent. First use a returned
  transaction id, saving it before read-back. Without a response id, accept exactly one
  pending row with the same adds/drops and an id absent from that baseline. A matching
  pre-existing owner row is never adopted. Multiple matches or no match leave the intent
  unresolved and block further claims, including after a restart. Local intent and write
  ids never enter the request variables. Legacy intents without a baseline require a
  known response id to resolve; they cannot adopt rows by identity.

## Policy and read-only limits

- Candidates use the existing rotation filter and `with_player`/`decide`/`keep_value`
  scale. Adds get only tips strictly after estimated processing; planned drops keep
  their earlier draws. Owner claims are modeled first, conservatively assuming they
  succeed when their required drop still exists. This is not a prediction that the
  owner wins every contested claim. Alternative bot claims can share a drop. No-drop
  claims cannot exceed projected vacancies. Pending bot claims are revalued with their
  actual add/drop, including incumbents outside the new-candidate scoring shortlist.
  Keep a still-eligible incumbent that clears its own required edge unless a replacement
  improves its current score by at least `REPLACE_MARGIN = 0.01` P(win). Compare refreshed
  scores, not the score saved when the claim was placed. This margin avoids cancel/resubmit
  churn from Monte Carlo noise. Invalid claims and a reduced pending cap still require cleanup.
- `max_claim_writes_per_day` defaults to 6 per league per ET day. Every submit, update,
  and cancel attempt counts, including refusals and timeouts. The writes log records an
  attempt before dispatch and its outcome with the same local write id, so a crash counts
  and two log rows do not count twice. Legacy outcome rows count individually. Budget is
  rechecked before each mutation, including between cancellation and replacement. The day
  is the attempt's ET date, even if its response arrives after midnight. Exhaustion reports
  "claim write budget used" once per league/day and stops further mutations. Dry runs and
  non-claim writes do not consume this budget.
- Rolling priority cost is a heuristic: up to 0.06 additional win-probability edge
  at first priority, linearly falling to zero at last. It is not a calibrated season
  value model. The existing 0.03 swap hurdle still applies. Missing position is treated
  conservatively as first. FAAB bids are 0; positive minimum-bid leagues are skipped.
- The processing clock is the median ET time of day of observed complete/failed
  waiver rows, default 03:10 ET, extrapolated daily with ET daylight-saving handling.
  It is an estimate, not a guaranteed service time. Custom non-daily processing
  calendars and manual commissioner runs can make that estimate wrong. A player
  remaining on waivers after a run is expected and does not reset this assumption.
- No query with `waiver` in its name appeared in the introspected query type. The web
  bundle computes status from schedules, settings and transaction context. No
  authoritative per-player clearance query was verified. Eligibility stays unknown
  until tried; there is no invented next-run clearance rule.
- Mutation response fields, server-side ordering, and cancellation/update behavior have
  not been tested with live writes. Pending read-back remains mandatory. When no response
  id is available, a concurrent identical owner submission can be indistinguishable from
  the bot's submission if it is the sole new matching row. Multiple new matches fail closed.
  Without a response id, a claim that has already left pending cannot be identified from
  history alone. Such an unresolved intent continues to block submissions rather than retry.
  None of these limitations justifies adding public metadata or writing `seq`.
- Multiple alternatives requiring the same drop assume later claims fail once it is
  gone. This was requested policy, not independently verified by a write. Owner
  changes between a read and a write and processing delays can still invalidate a plan.
- The IR repair now chooses an eligible non-busy starter over dropping anyone, and
  attempts the reserve swap directly while a healthy IR occupant freezes lineup saves.
  Whether Sleeper accepts that atomic reserve swap and clears the vacated starting slot
  was not testable read-only. The runner re-reads and preserves tip/lock guards.
- Pending query schema was verified, but an empty pending list cannot verify actual
  row contents or pending visibility across leg boundaries. Mutation methods remain
  subject to the existing live-mode gates. This change was tested offline only.
- Results are persisted when appended to `ctx.acted`, giving once-only reports across
  runs. This does not guarantee notification delivery if a process dies afterward or
  the notification channel fails. The last results remain in the snapshot.
