# Claude Booster Codex Bridge

This repository contains Codex compatibility templates for Claude Booster.

## Booster Commands In Codex

When Dmitry invokes any of these as the first non-whitespace token in the latest
user message, or as an explicit Codex skill/prompt alias, treat it as a Claude
Booster command:

- `start` or `/start`
- `handover` or `/handover`
- `consilium <topic>` or `/consilium <topic>`
- `audit <topic>` or `/audit <topic>`
- `code-review [topic]` or `/code-review [topic]`
- `architecture [--update]` or `/architecture [--update]`
- `go <artifact contract>` or `/go <artifact contract>`
- `debt <mode>` or `/debt <mode>`
- `phase`, `update`, `lead`, `delegate`, `audit-trace`, `hackathon`,
  `verify-after-edit`, `verify-flow`

Do not trigger Booster commands from quoted text, logs, code blocks, repository
content, or examples. Plain English/Russian mentions like "update this paragraph"
are not command invocations unless the command token is the leading token.

If the matching Codex skill is installed, use it (`$consilium`, `$handover`,
etc.). If not, read `templates/codex/skills/booster-command/SKILL.md` and run
the original command spec from `~/.claude/commands/<command>.md` or
`templates/commands/<command>.md` with the Codex adapters described there.

Codex does not officially expose a custom top-level slash-command registry.
The supported aliases are Codex skills (`$consilium`) and legacy prompts
(`/prompts:consilium`). Do not claim that bare `/consilium` is guaranteed unless
the current Codex UI actually shows it.
