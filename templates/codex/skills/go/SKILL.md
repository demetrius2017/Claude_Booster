---
name: "go"
description: "Run the Claude Booster go protocol in Codex: Flow Designer, Challenge, Prototype Gate, Worker, independent direct-probe verification, final deploy regression gate, diff review, and verdict from an Artifact Contract. Supports opt-in `go fable` Quality Chair mode."
---

# Booster Go

Read the sibling skill `../booster-command/SKILL.md`, then run command `go`
through that runner.

Treat the rest of the user message as `[fable] <Artifact Contract>`. If the
first token is `fable`, run the command's opt-in Семёрка-F mode: Fable is a
read-only Quality Chair for Challenge and final watchlist closure, not Lead,
Worker, Verifier, or a default route. During implementation the Verifier returns
an evidence receipt from direct read-only probes; it does not create or rewrite
test artifacts. Durable regression tests are created or updated only at the
final deploy gate, after the direct probes pass.

For every non-trivial behavioral, data, runtime, external-system,
incident-driven, or critical-component Prototype Gate, require the Prototyper's
mandatory investigation notebook before Worker. It records direct authorized
read-only commands/queries, source identity and environment, ISO timestamp or
window, filters, counts/samples, expected versus actual, invariant result, and
raw-output reference. It is durable under `notebooks/` or `reports/prototypes/`,
never tempdir; large raw output is repo-relative with SHA-256. It is an investigation journal, not a synthetic test
stand; a paired probe script is not required. Notebook N/A is permitted only
when the entire gate is explicitly N/A for a pure docs/format/static-config task
with no executable data/runtime hypothesis and a concrete reason.
