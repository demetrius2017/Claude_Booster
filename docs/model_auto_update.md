# Automatic model updates within a class

Booster discovers candidates daily on SessionStart, in a separate process. The
session keeps its existing route while qualification runs. `model/list` from the
installed Codex App Server supplies account/client-visible candidates and effort
support. Claude aliases discover the recommended version supported by the installed
CLI. Keep provider CLIs up to date: an old Claude CLI can still resolve `opus` to
an older release even when a newer version has been announced.

Policies use version-independent families: Codex Luna = fast, Sol/Terra = balanced,
Astra = flagship; Claude Opus, Sonnet and Haiku remain separate classes. A new
numeric version must stay within its class and provider. Catalog order and
`isDefault` do not imply quality. Unknown families are held, visibly, until their
class semantics are supported. PAL, Z.ai and Grok currently remain on their
existing routes and are reported as unsupported by this updater; their provider
and entitlement contracts are not interchangeable with the Codex catalog.

Before activation, the exact candidate must complete a read-only fixture tool
exercise plus deterministic arithmetic and code-expression checks. A random file
nonce proves it read the fixture. Claude modelUsage and Codex CLI's model header
must match the requested ID. No Booster worker fallback is allowed in the probe.
This is a compatibility/regression gate, not a benchmark proving global quality.
Failures preserve the previous verified receipt and current route. The initial
Claude version is qualified too. If that check fails, its existing concrete ID is
still frozen in the alias mapping to prevent an unqualified future alias jump;
the route is not marked verified.

Claude Agent accepts aliases, not concrete IDs. Booster therefore stores the
verified model in `settings.json` env `ANTHROPIC_DEFAULT_<TIER>_MODEL`. The hook
allows an alias only when its inherited mapping matches the verified route.
Restart existing Claude sessions after an update; their environment may retain
an earlier mapping. The main session's `model` setting is not changed. User-owned
alias mappings and conflicting explicit pins hold the update instead of being
overwritten. Discovery strips inherited alias mappings, so a prior verified pin
does not prevent finding the next version.

Set `selection_mode: "pinned"` on a routing entry in `~/.claude/model_balancer.json`
to opt out. Legacy migration, scorer, qualification and health fallback respect
this explicit pin. An explicit command model still follows that command's contract.
Set `CLAUDE_MODEL_AUTO_UPDATE_DISABLE=1` to stop background discovery; already
verified routes remain usable. Supported auto routes are protected against the
historical scorer reverting them after qualification.

State and receipts: `~/.claude/state/model_auto_update.json`. Each receipt records
suite, exact source fingerprints, target and check timestamp. Manually changing
provider/model/effort invalidates an old receipt. Writes are atomic; one refresh
runs at a time; balancer updates are serialized. Last successful receipts remain
usable across offline days. Daily retry is bounded and failures are visible.

Commands:

```sh
python3 ~/.claude/scripts/model_auto_update.py status
python3 ~/.claude/scripts/model_auto_update.py refresh --force
python3 ~/.claude/scripts/model_balancer.py get coding
```

The SessionStart summary reports check time and candidate/status per category.
An asynchronous result applies to subsequent worker launches; it does not replace
the model of a running worker. Tests can override the state/settings paths with
`CLAUDE_MODEL_AUTO_UPDATE_STATE` / `CLAUDE_MODEL_AUTO_UPDATE_SETTINGS` and the
balancer path with `CLAUDE_MODEL_BALANCER_PATH`.
