# Same-day looks

Implement the supplied same-day policy and visible-plan spec in the `sameday`
worktree. No commits, Sleeper mutations, or personal data in repository artifacts.

Design: retain game-day keys for model callers, add each draw's actual tip, and
solve slots over sorted distinct tip events. Every chosen draw jumps over
overlapping events. Use GAME_SPAN=150, BOX_DELAY=10, LEAD=10 minutes and the
first :10/:40 run after the box deadline. A 90% normal branch and a 10% delayed
branch (skip the rest of that day) explicitly price uncertain same-day handoffs.
This probability is a conservative assumption, not fitted timing data. Both
realization and simulation follow these same transitions. Opponents retain
one look per day. Existing runtime held/locked guards remain authoritative.

- [x] Add parameterized timing, runtime handoff, terminal and Monte Carlo tests.
- [x] Carry tips from week and pickup construction; implement event DP,
      realization, simulation and immediate lineup selection.
- [x] Serialize ordered nominal looks, continuation bars and expected slot value;
      describe bench roles without claiming an unmodeled injury backup.
- [x] Reuse presentation for status and briefs; extend the existing whiteboard
      rows and fictional demo, preserving old snapshot keys.
- [x] Verify full offline suite, read-only status, 390px demo and desktop browser
      behavior; review the final diff. No commits.

Review focus: simultaneous tips, cadence boundaries, missing boxes, frozen bench
players, passed final-game fallback, zero-play terminal draws, slots moved by
respecify_locks, and legacy snapshots without plan fields.

Verification: offline fixtures cover the cadence boundary, one-hour overlap,
three-hour handoff, long games and missing boxes, terminal DNPs, frozen bench
players, and plans following displaced occupants after lock respecification.
Independent review found no important issues; additional randomized event
fixtures matched realization exactly and simulation within Monte Carlo error.
Read-only status and local Chromium Board checks completed without Sleeper writes.
The brief deliberately omits EV to keep each slot to one short line; status and
the Board include it. Browser checks used a disposable profile because the
Browser plugin was unavailable. No commits were made.
