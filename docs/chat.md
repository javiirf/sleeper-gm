# Ask your GM

The Ask tab answers questions using the last local league snapshot, its lineup and game plan,
pending claims, the last 40 decisions, week reviews, saved instructions and recent conversation.
It can explain a pass, compare the current lock bar, or remember an owner instruction. It does
not fetch fresh league data. Run the manager to refresh stale information.

Chat starts off. In your XDG config file, normally `~/.config/sleeper-gm/config.json`, add:

```json
{
  "chat": {
    "command": ["claude", "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config"],
    "timeout": 90,
    "history": 24,
    "per_hour": 30
  }
}
```

Install and authenticate the model CLI separately. The command reads the complete prompt from
stdin and returns plain text on stdout. A string command is split with `shlex`; an argument
array avoids quoting surprises. Neither form runs through a shell, so pipes and redirects are
not supported. Nonzero exit status returns a generic error; model stderr is never exposed.
Timeout returns HTTP 504 and kills the process group on POSIX systems.

The corresponding Codex stdin/stdout command shape is:

```json
["codex", "exec", "--sandbox", "read-only", "-"]
```

**The model must have no tools.** Opponent names and other public league data are untrusted.
The prompt labels them as data and removes raw crowd messages and extracted news, retaining only
numeric crowd aggregates. Those prompt boundaries are defense in depth, not a tool sandbox.
The Claude example disables built-in tools and uses strict MCP configuration. Ensure that no
MCP tools are enabled by your launcher. Codex's `read-only` sandbox does **not** disable tools:
the Codex example alone is not a tool-free setup. Before using it for chat, independently disable
all built-in and MCP tools in your model launcher, or use a wrapper that calls a text-only model.
The server does not inspect or enforce a CLI's tool configuration. The selected model provider
receives the league context and owner messages sent through your command.

The GM is prompted to speak briefly, use actual numbers, admit unknowns, and avoid invented
facts. Its prose remains model output. Only the server-validated instruction list counts as saved.

## Saved instructions

Ask naturally, for example: "Keep Starter One", "Avoid Free Three", "Lock Starter One at 35
this week", or "Remember that I prefer to keep the last roster spot flexible."

| Instruction | Effect on later manager runs |
|---|---|
| `keep` | Never drop this player, including IR fixes and claim drops |
| `avoid` | Never add this player through free agency or claims |
| `lock_at` | Use this finite, nonnegative points threshold for an owned player this week; game eligibility still applies |
| `unkeep`, `unavoid`, `unlock_at` | Remove that player's matching instruction |
| `note` | Save up to 200 characters for later chat context; does not change policy |
| `pause`, `resume` | Change the global live setting used by `sleeper-gm off` and `sleeper-gm on` |

Chat itself never calls Sleeper or saves a lineup, lock, claim or trade. Pause/resume affect all
managed leagues and do not start a run. Saved roster and lock instructions apply on later runs;
they do not reverse already final locks or already completed transactions.

Player ids must exist in local league/player data. The server supplies names, so model-provided
names cannot relabel another player. Only owned players accept lock thresholds. Weekly thresholds
carry the snapshot's season and week, expire at Monday 00:00 Eastern, and cannot be created from a
snapshot whose week has ended. Keep, avoid and notes remain until removed.
An owner's threshold replaces that player's displayed nonterminal lock bars and is used when
valuing later looks. Below the threshold, the player waits. If never locked, his team's final game
still counts, including zero if he sits.
If the week changes or expires while the model is replying, weekly lock instructions are refused
instead of being carried into the new week. Player ownership is checked again after the reply.

Accepted instructions appear as inline lines with Undo. Every replacement receives a fresh id; an old
Undo cannot remove its replacement. Rejected actions show a reason. Clear removes conversation
history and leaves instructions and rate-limit accounting in place.

For a custom model wrapper, the optional final action block is:

````text
That threshold uses the owner's requested number.
```json
{"actions":[{"kind":"lock_at","player_id":"123","points":35}]}
```
````

The last fenced JSON block is removed from the reply. Actions are parsed only when there is one
JSON fence and it ends the reply; multiple blocks or trailing prose cannot apply actions. Invalid JSON is ignored.
Only the kinds in the table are accepted; unexpected fields and invalid values are refused.
Instructions are stored atomically in the league's `directives.json`. Redacted conversation rows
(`t`, `role`, `text`, `actions`, plus assistant refusals) live in `chat.jsonl`. Rate accounting lives
separately in `chat-rate.json`. These files stay in XDG state, never the repository.

## Private access and limits

The server still binds to localhost by default. Keep it behind an authenticated proxy such as
Tailscale Serve. To restrict the API to specific Tailscale identities, add an optional allow list:

```json
{"chat":{"allow":["owner@example.test"]}}
```

The proxy must supply a trusted `Tailscale-User-Login` header and block untrusted direct access
to the backend. A plain client can forge this header if it can reach the backend. When configured,
the allow list applies to API reads as well as chat and instruction mutations. An empty list
denies everyone. Without a list, access control comes from your private network/proxy.

POST requests must use JSON, fit within 8 KB, and contain at most 1000 characters of message text.
A supplied Origin must match Host. Only one chat or instruction mutation runs at a time per league;
concurrent chat, Clear and Undo return HTTP 409. The default limit is 30 model attempts per league
per rolling hour, including failed attempts. HTTP 429 reports exhaustion. The default prompt
includes the latest 24 history rows. History remains visible until cleared.

Endpoints use `?league=<id>`: GET/POST `/api/chat`, POST `/api/chat/clear` with `{}`, and POST
`/api/directives` with `{"id":"<directive-id>"}` to undo. GET `/api/directives` lists active instructions.
Disabled chat returns HTTP 503: `Chat is off. Set chat.command in your config.`

`sleeper-gm web --demo` uses a fixed answer based on the demo snapshot and in-memory history.
It runs no model command and writes no production history, instructions or live configuration.
