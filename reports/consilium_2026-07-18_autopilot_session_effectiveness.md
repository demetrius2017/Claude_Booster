# Consilium: Autopilot Session Effectiveness

**Date:** 2026-07-18  
**Decision:** **MODIFY** (Fable 5, final judge)

## Task context

The council reviewed how Claude Booster can make long Codex autopilot sessions more effective, using the audited `electro-estimate-ai` session as the failure case. The scope is deliberately limited to improvements Booster can implement. Host-owned changes that require control of Codex internals are excluded from the recommendation.

The session produced useful review findings and six documentation commits, but accumulated 105 agent spawns, 830 `wait_agent` calls, 42 modified tracked files (about `+3504/-861`) plus untracked files, no implementation commit, and drift from sealed money evaluation into UI, catalog, OCR, fusion, and analogs. Goal activation happened only after the user's “и?” intervention; that activation defect has since been addressed in the Booster autopilot contract.

## Verified Facts Brief

Current repository evidence shows that Booster already has several sound pieces:

- `templates/commands/autopilot.md`, `templates/codex/skills/autopilot/SKILL.md`, and the Codex adapter now require `get_goal`, creation of a matching persistent goal, preservation of unrelated active goals, and immediate continuation into the first North-Star step.
- `templates/scripts/fable_autopilot_state.py` and `templates/scripts/fable_autopilot.py` provide atomic project-scoped autopilot state, a three-call Fable budget, typed verdicts, and `plan_complete`, conditional `first_slice`, and `final_diff` checkpoints.
- `templates/scripts/phase.py` and `phase_gate.py` model and partially enforce `RECON → PLAN → IMPLEMENT → AUDIT → VERIFY → MERGE` for Claude-hooked work.
- `delegate_gate.py`, `/go`, `require_task.py`, `require_evidence.py`, and `verify_gate.py` provide delegation, artifact-contract, and evidence controls in defined surfaces.
- Existing telemetry covers evidence density, review hygiene, cadence, provider health, context occupancy, and other session signals.

The missing control is a transaction boundary around one implementation slice. No current invariant couples the goal to one slice, bounds work in progress, attributes dirty paths, requires verified implementation closure before a new domain, detects duplicate spawning, budgets polling, or exposes spawn/wait/dirty-delta/commit-class telemetry. `verify_gate.py` protects handover commits, not every ordinary implementation package. `delegate_gate.py` limits direct Lead work, not excessive delegation.

The code therefore supports durable direction and phase language, but not the equivalent of a database transaction around one roadmap slice. That gap explains how useful activity can coexist with an expanding, ambiguously owned worktree and no implementation delivery.

## Explicit control boundary

### Booster can control or measure

- Its own commands, skills, scripts, state files, prompts, worker wrappers, and Claude Code hooks.
- Atomic project-local slice state and append-only event history.
- Git baseline capture, path hashes, conservative ownership claims, exact diff/tree hashes, and verification evidence.
- WIP gates for Claude hooks and Booster-managed wrappers.
- Path-based artifact contracts, scope warnings, and an off-scope backlog.
- Spawn/retry/wait accounting for Booster-managed workers.
- Post-hoc Codex session JSONL telemetry, provided schema coverage and unknowns are reported honestly.
- Compact slice receipts generated from ledger, git, and verification facts.

### Booster cannot control and must not claim to control

- Native Codex `spawn_agent`, `wait_agent`, `followup_task`, or `interrupt_agent` calls.
- Codex scheduling, concurrency, goal continuation internals, transcript compaction, context-window behavior, or tool UI.
- Claude Code's opaque built-in `/goal` from a Booster script.
- Another live Codex session's in-memory plan when the host does not expose it.
- User or another client edits to the same worktree.
- Authorship from a diff or hash alone, safe automatic separation of concurrent edits to one file, or complete causality after lossy compaction.

For native Codex, the honest product is executable shared state, wrapper checks where invoked, and post-hoc diagnostics—not universal hard enforcement.

## Council positions

| Council member | Position | Key judgment | Verdict |
|---|---|---|---|
| Architect | Build around A+E+B+F: slice ledger, verified closure, baseline attribution, then path-scope control | The missing primitive is a transaction boundary, not more autonomy instructions | PASS with staged architecture |
| Control engineer | Couple WIP, spawn/wait observation, path ownership, and closure to one typed slice | Every control needs observable state, terminal dispositions, and promotion criteria; polling thresholds must be empirical | PASS with measured promotion |
| Adversarial auditor | Ship only a small fail-closed protocol; keep transcript telemetry diagnostic | Ledger claims must lose to git/filesystem facts; never auto-commit foreign or ambiguous paths; never market Codex observation as enforcement | CONDITIONAL PASS |
| GLM-5.2 external | Start with telemetry, baseline capture, advisory closure, and backlog before enforcing | Establish measurement validity and attribution error rates before hard gates | MODIFY / stage first |
| Fable 5, final judge | One MVP shipment, then evidence-gated enforcement phases | Separate atomic ledger plus event log; conservative baseline; typed advisory closure bound to exact hashes; path drift advisory; transcript telemetry diagnostic | **MODIFY** |

## Strongest counterargument

The ledger can become a second, stale source of truth beside git and the filesystem. A crash, wrapper bypass, concurrent session, or compaction can leave it confidently wrong; subsequent agents may then trust the control intended to prevent false confidence.

The answer is architectural: the ledger records **claims**, while git and filesystem observations are **facts**. On mismatch, facts always win, the slice becomes `ambiguous` or `quarantined`, and automation stops. Recovery must be explicit, provenance-preserving, and testable. The ledger must never infer ownership merely because a file changed after activation.

## Final decision: MODIFY

Do not immediately install hard WIP, spawn, or wait gates. Ship one observational and conservative-attribution MVP first. Promote only controls whose false-positive rate and overhead are acceptable in real sessions.

The MVP is one coherent shipment:

1. A separate atomic `state/slice_ledger.json` plus append-only event log.
2. Activation baseline from git porcelain plus HEAD/tree and scoped content hashes; every baseline-dirty path is foreign by default.
3. Advisory typed closure—`committed | quarantined | delivered_uncommitted | blocked`—bound to the exact diff/tree hash and verification result.
4. Deterministic path-scope drift detection with append-only backlog routing; semantic classification remains advisory.
5. Diagnostic transcript telemetry for spawns, waits, dirty deltas, commit classes, and timing, with parser coverage and `unknown` states.

After at least 10 measured sessions and attribution false positives below 15%, Phase 1 may enforce WIP=1 only through Claude hooks and Booster wrappers. Phase 2 may add wrapper-only spawn/wait decisions based on empirical distributions. The council explicitly rejects a hard-coded “three waits” rule before measurement.

## Complete improvement inventory

| Improvement | Stage | Verdict | Scope and constraint |
|---|---:|---|---|
| Atomic slice ledger with typed lifecycle and `run_id` | MVP | SHIP | Atomic acquire/rename; host, PID/session metadata, recoverable stale ownership; ledger claims never override git facts |
| Append-only event log | MVP | SHIP | Records activation, ownership claims, worker attempts, observations, verification, closure, and recovery provenance |
| Activation HEAD/tree/porcelain baseline | MVP | SHIP | Baseline-dirty and overlapping paths are foreign/ambiguous; hash only relevant scoped paths, not entire large or secret untracked trees |
| Artifact contract and allowed-path set | MVP | SHIP | Deterministic path boundary; dependencies can be explicitly added with reason and event provenance |
| Conservative worktree attribution | MVP | SHIP | Baseline-clean changed allowlisted paths are candidates; dirty overlap or concurrent mismatch is never auto-owned |
| Typed closure bound to exact diff/tree hash | MVP | SHIP | `committed`, `quarantined`, `delivered_uncommitted`, or `blocked`; evidence must refer to the exact candidate state |
| Verification evidence and commit classification | MVP | SHIP | Separate source/test/docs/report changes; flag docs-only commits while implementation delta remains open |
| Path drift to append-only backlog | MVP | SHIP ADVISORY | Hard facts from paths; semantic topic drift is a warning, never the sole blocker |
| Session-efficiency telemetry | MVP | DIAGNOSTIC | Activation delay, first worker/verification/implementation commit, spawns, waits, dirty delta, commits by class, drift, parser coverage/unknown rate |
| Compact slice handoff | MVP | SHIP | Generated from ledger + git + evidence; separates facts, claims, and unknowns; bounded size |
| WIP=1 gate | Phase 1 | PROMOTE IF PROVEN | Only Claude hooks and Booster wrappers; requires ≥10 sessions and attribution FP <15% |
| New-slice closure gate | Phase 1 | PROMOTE IF PROVEN | Block wrapper-managed next slice until current disposition; no universal native Codex claim |
| Spawn role/brief/retry accounting | Phase 1 | SHIP IN WRAPPERS | Attempt IDs, role independence, brief hashes, recorded failure reason; max retries may be policy-driven after data |
| Duplicate-brief guard | Phase 1 | PROMOTE IF PROVEN | Applies only to Booster-managed spawning; override requires new evidence and provenance |
| Spawn/wait intervention | Phase 2 | EXPERIMENT | Wrapper-only decisions based on observed progress/events; no arbitrary fixed threshold |
| Semantic drift classifier | Later | ADVISORY ONLY | May help triage ambiguity but cannot hard-block work |
| Live efficiency status in `/start`, autopilot status, handover | After MVP | SHIP | Surface diagnostic trends with coverage and unknowns, not agent rankings |

## Rejected or uncontrollable proposals

- Intercepting or limiting native Codex collaboration tools.
- Modifying Codex's scheduler, concurrency, compaction, context implementation, or UI.
- Programmatically controlling Claude's opaque `/goal`.
- Promising the same hard enforcement in Claude and Codex.
- Automatically proving authorship from git diffs or hashes.
- Automatically committing any baseline-dirty, overlapping, or ambiguous file.
- Treating semantic scope classification as a hard gate.
- Reconstructing complete truth from incomplete or compacted transcripts.
- Adding a daemon, service, database, dashboard, or new scheduler to the first version.
- Optimizing commits/hour, agents/session, raw waits, line count, test count, clean-worktree rate, or backlog count as standalone KPIs; each can reward theater.
- Hard-coding “three unchanged waits” before reliable event semantics and empirical baselines exist.

## MVP architecture and implementation order

1. **Schema and invariants:** define slice identity, typed states, disposition, claim-versus-fact semantics, versioning, and recovery rules.
2. **Atomic persistence:** implement `state/slice_ledger.json`, append-only event log, locking/acquisition, stale-owner detection, and one-command recovery.
3. **Git baseline adapter:** capture HEAD/tree, porcelain state, scoped hashes, foreign baseline paths, and concurrent mismatches.
4. **Attribution and artifact contract:** calculate candidate-owned, foreign, ambiguous, and off-scope paths; never auto-resolve ambiguity.
5. **Closure:** bind verification and terminal disposition to exact diff/tree hash; allow commits only from explicitly attributed paths where the wrapper performs the commit.
6. **Backlog and handoff:** append off-scope discoveries with source slice/reason; generate a compact fact/claim/unknown receipt.
7. **Telemetry adapters:** parse Claude/Codex evidence with schema version, coverage ratio, and explicit unknowns; establish baselines over at least 10 sessions.
8. **Promotion review:** enable WIP=1 only on Claude hooks/Booster wrappers if acceptance thresholds pass; experiment with spawn/wait decisions later.

No implementation phase should widen the host-control claim beyond the explicit boundary above.

## Acceptance criteria and KPIs

Primary outcome criteria:

- At least 80% of measured autopilot sessions produce a verified implementation commit before work begins in a new roadmap domain, unless the slice has an explicit non-commit terminal disposition.
- Zero unattributed paths at closure; every changed path is attributed, foreign, quarantined, or explicitly excluded.
- Zero foreign or baseline-dirty paths included in an automatic/wrapper-managed commit.
- Ledger repair is required in fewer than 20% of slices.
- Attribution false-quarantine rate is below 15% before WIP enforcement is promoted.
- Median orchestration overhead remains below 10% of slice time, or demonstrates a measured reduction in ambiguity/scope breaches sufficient to justify it.
- Documentation-only commits while a dirty implementation package remains open decrease materially from the baseline.

Diagnostic, non-gameable bundles—not standalone rankings—should include:

- First-pass acceptance against the exact diff/tree hash.
- Ambiguous overlap count and attributed-path coverage.
- Active-slice age and terminal-disposition coverage.
- Scope breaches and off-scope discoveries routed to backlog rather than acted upon.
- Time to first verified implementation commit.
- Spawns and waits per terminal verified slice, accompanied by progress-event coverage.
- Rework after verifier rejection.
- Transcript parser coverage and unknown rate.

## Kill criteria

Stop rollout or revert enforcement immediately if any of these occurs:

- One foreign or baseline-dirty file enters an automatic/wrapper-managed commit.
- A stale ledger lock blocks valid work and cannot be recovered by one documented command.
- Ledger reports `committed` or `verified` while the exact diff/tree hash does not match.
- A concurrent session silently overwrites ledger ownership.
- Post-hoc Codex observation is presented as native hard enforcement.

Stop or redesign after the evaluation window if:

- More than 20% of slices need manual ledger repair.
- More than 15% of legitimate changes are falsely quarantined.
- Median overhead exceeds 10% without measurable reduction in ambiguous attribution or scope breaches.

## Risks

- **Stale authority:** mitigated by claim-versus-fact precedence, atomic state, typed ambiguity, and explicit recovery.
- **Concurrent sessions:** mitigated by run/session identity and fail-closed collision handling; same-file concurrency remains uncontrollable.
- **False attribution:** mitigated by treating baseline dirt as foreign and refusing automatic resolution.
- **Metric gaming:** mitigated by bundled outcome metrics, exact-hash acceptance, and diagnostic-only orchestration counts.
- **Parser drift:** mitigated by versioned adapters, coverage ratios, fixtures, and explicit `unknown` rather than fabricated zeroes.
- **Excess process:** mitigated by a file/CLI MVP, no daemon/database/dashboard, bounded handoffs, and overhead kill criteria.
- **False parity:** mitigated by separate capability statements for Claude hooks, Booster wrappers, and native Codex.
- **Overblocking legitimate dependencies:** mitigated by reasoned path-contract expansion and advisory-only semantic classification.

## Implementation recommendations

Implement the MVP as a small, independently testable control module rather than extending the existing autopilot state blob. Keep directional autopilot state and per-slice transactional state separate: the North Star is durable strategy; the slice ledger is short-lived execution authority.

Use fail-closed guards at each data boundary: validate ledger schema and state transitions on input, preserve atomicity and idempotency during updates, and validate exact hashes plus terminal invariants before closure. Recovery must append provenance rather than erase history.

Build fixtures for clean baseline, baseline-dirty overlap, untracked paths, crash/stale owner, concurrent acquisition, changed verification hash, partial quarantine, and parser schema drift. Test the uncontrollable boundary explicitly: native Codex activity without a Booster wrapper must appear as unobserved/diagnostic—not as controlled.

Finally, treat the first ten sessions as an instrument-validation period. The next architectural decision is not whether stricter control sounds desirable; it is whether the MVP can attribute work accurately, cheaply, and without passing foreign changes downstream. Only then promote WIP=1 and wrapper-level orchestration decisions.
