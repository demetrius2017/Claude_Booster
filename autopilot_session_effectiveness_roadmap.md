# Autopilot Session Effectiveness Roadmap

**Status:** Phase 0 mechanics installed; prospective calibration in progress
**Decision authority:** Consilium 2026-07-18; Fable 5 final verdict `MODIFY`  
**Scope:** Claude Booster-managed commands, wrappers, hooks, files, and diagnostic adapters

## North Star

Make every Booster-managed autopilot session behave like a bounded implementation transaction: one explicit roadmap slice enters with a known git baseline and artifact contract, produces verifiable implementation evidence against an exact repository state, and reaches one typed terminal disposition before another domain begins.

The final goal is that useful exploration reliably becomes a verified implementation delivery without silently consuming foreign work, losing provenance, or claiming control over host capabilities that Booster does not own.

## Control boundary

### In scope

- Atomic, project-local slice state and append-only event history.
- Git HEAD/tree/porcelain baselines and scoped content hashes.
- Conservative classification of candidate-owned, foreign, ambiguous, and off-scope paths.
- Artifact contracts, allowed-path sets, exact-hash verification, typed closure, backlog routing, and compact handoffs.
- WIP gates in Claude Code hooks and Booster-managed wrappers after promotion criteria pass.
- Spawn, retry, wait, progress, dirty-delta, commit-class, and timing observations for Booster-managed workers.
- Post-hoc Codex transcript diagnostics with explicit parser coverage and `unknown` states.

### Out of scope

- Intercepting or enforcing native Codex `spawn_agent`, `wait_agent`, `followup_task`, or `interrupt_agent` calls.
- Modifying Codex scheduling, concurrency, compaction, context-window behavior, goal continuation internals, or UI.
- Programmatic control of Claude Code's opaque built-in `/goal`.
- Inferring authorship from a diff/hash, separating concurrent edits within one file, or automatically committing baseline-dirty or ambiguous files.
- Semantic drift as a hard gate or complete causality reconstruction from lossy transcripts.
- A daemon, dashboard, database, service, or new scheduler.

Git and filesystem observations are facts. Ledger entries are claims. Whenever they disagree, facts win and the slice becomes `ambiguous`, `quarantined`, or `blocked`; automation must stop rather than repair the discrepancy by inference.

## Audited baseline

The initial baseline is the audited `electro-estimate-ai` autopilot session:

| Signal | Baseline |
|---|---:|
| Session duration | ~9.5 hours |
| Agent spawns | 105 |
| `wait_agent` calls | 830 |
| Modified tracked files | 42 |
| Tracked diff | ~`+3504/-861` |
| Useful commits | 6 documentation commits |
| Verified implementation commits | 0 |
| Domain behavior | Drifted from sealed money evaluation into UI, catalog, OCR, fusion, and analogs |
| Goal activation | Required user intervention (`и?`); contract defect subsequently fixed |

This is a failure baseline, not a universal performance distribution. Spawn and wait limits remain diagnostic until at least ten instrumented sessions establish valid event semantics and empirical ranges.

## Current evidence state (2026-07-19)

Phase 0's ledger, exact-state closure, recovery, calibration registry, root-session identity binding, telemetry runtime, and Booster/Codex command contracts are implemented and installed. The implementation is carried by commits `68fc40e10b7ae21265184346209766c1476012e9`, `66dac05231423d9e3f3a61fa4002e44a0b6e3d3c`, `33fd7bd7fd944b06d45ca332c4ae3fb5cc0c0d73`, `dde13fa9f33cdba73375929d0d3a5fd9f01ceee4`, and `4481f00950ad141e4759dbff558edb623cd6e96d`. The installed runtime and three updated command contracts were byte-identical to those sources, and the installation completed successfully with a retained backup.

The first prospective specimen is recorded separately as **1 attempted session and 0 promotion-eligible sessions**. It closed `blocked`, originated before the corrected root-session contract and therefore carries legacy/wrong-root identity evidence, has unavailable controls, and has neither a valid telemetry receipt nor a human calibration label. It must not enter a promotion denominator or be repaired by backfill.

The clean sealed calibration window therefore remains **0/10 eligible sessions**. Phase 1 and Phase 2 remain gated and have not started; none of their enforcement or orchestration claims is active.

### Exact next-session procedure

1. Start a genuinely new top-level Codex session and retain its unique root `payload.session_id`; do not reuse a thread ID, subagent ID, or the blocked specimen's identity.
2. Before implementation work, bind the leading `session_meta` transcript row to the new run:

   ```bash
   python3 ~/.claude/scripts/slice_calibration.py --cwd "$PROJECT_ROOT" session-start \
     --run-id "$RUN_ID" --session-id "$ROOT_SESSION_ID" \
     --provider codex_rollout_v1 --artifact-domain implementation \
     --expected-control ledger --expected-control git \
     --expected-control verification --expected-control closure \
     --transcript "$CODEX_TRANSCRIPT"
   ```

   Work may begin only if this command exits `0`; a thread/root mismatch is a byte-stable rejection, not a reason to retry with the thread ID.
3. Complete the normal ledger, verification, terminal, and domain-outcome sequence, then record telemetry against the same immutable run/root pair:

   ```bash
   python3 ~/.claude/scripts/slice_telemetry.py --cwd "$PROJECT_ROOT" record \
     --provider codex_rollout_v1 --transcript "$CODEX_TRANSCRIPT" \
     --run-id "$RUN_ID" --session-id "$ROOT_SESSION_ID"
   ```

4. A human reviews every classified path and supplies the labels file; only then record calibration:

   ```bash
   python3 ~/.claude/scripts/slice_calibration.py --cwd "$PROJECT_ROOT" record \
     --run-id "$RUN_ID" --session-id "$ROOT_SESSION_ID" \
     --labels-file "$HUMAN_LABELS_JSON"
   ```

Only a session with valid root-bound activation, terminal evidence, telemetry, and human labels increments the clean sealed `0/10` promotion counter.

## KPI contract

| KPI | Definition / formula | Baseline | Target | Measurement source | Promotion threshold | Kill / redesign threshold |
|---|---|---:|---:|---|---|---|
| Verified implementation before domain transition | Sessions with an exact-hash verified implementation commit before a new roadmap domain, or an explicit non-commit terminal disposition, divided by measured sessions | 0% implementation commits in audited session | >=80% | Slice events + git commits + artifact contract domain | >=80% across calibration window | No improvement after calibration window |
| Attributed-path closure coverage | Changed paths classified as candidate-owned, foreign, quarantined, or explicitly excluded / all changed paths at closure | Unknown; 42 tracked files plus untracked files | 100% | Git baseline adapter + closure receipt | 100% in every promoted slice | Any unattributed path accepted at closure |
| Foreign-path commit safety | Foreign or baseline-dirty paths included by wrapper-managed automatic commit | Not measurable in audited session | 0 | Baseline hashes + staged diff + commit tree | Zero events across calibration | One event: stop enforcement immediately |
| Attribution false-quarantine rate | Legitimate slice paths incorrectly quarantined / all legitimate slice paths reviewed | Unknown | <15% | Human-reviewed calibration labels + attribution events | <15% after >=10 sessions | >15% after evaluation window |
| Manual ledger repair rate | Slices requiring manual state repair / terminal slices | No ledger | <20% | Recovery events + terminal events | <20% after >=10 sessions | >20% after evaluation window |
| Orchestration overhead | Time spent in Booster slice control and bookkeeping / total slice elapsed time | Unknown | Median <10% | Monotonic event timestamps | <10%, or justified by measured ambiguity reduction | >10% without reduced ambiguity/scope breaches |
| Exact-state first-pass acceptance | Slices whose first verification passes for the exact diff/tree hash / verified slices | 0 verified implementation slices | Establish baseline, then improve | Verification events + hashes | Stable measurable denominator and improving trend | Ledger says verified/committed while hashes mismatch |
| Dirty implementation + docs-only commit incidence | Sessions with an open dirty implementation package when a docs-only commit lands / measured sessions | Present: 6 docs commits, no implementation commit | Material decrease; converge toward 0 | Git classification + active-slice state | Downward trend over >=10 sessions | Persistent or worsening after controls |
| Scope routing compliance | Off-scope discoveries appended to backlog rather than implemented / detected off-scope discoveries | Unmeasured; multiple domains entered | 100% for deterministic path drift | Artifact contract + backlog events | >=95% during calibration; 100% before hard gate | Off-scope work silently treated as owned |
| Parser observability | Parsed eligible transcript events / eligible events in reviewed fixtures; unknown rate reported separately | Unknown | Coverage explicitly reported; no fabricated zeroes | Versioned Claude/Codex adapter fixtures | Coverage sufficient for each promoted decision | Native Codex diagnostics represented as hard enforcement |
| Spawns/waits per terminal verified slice | Counts divided by terminal verified slices, paired with progress-event coverage | 105 spawns; 830 waits; denominator 0 | Empirical distribution only in MVP | Wrapper events + transcript diagnostics | Phase 2 only after stable coverage and >=10 sessions | No hardcoded threshold; rollback if intervention harms completion or increases false blocks |

Standalone counts such as commits/hour, agents/session, waits, lines, tests, clean-worktree rate, or backlog size are not success KPIs because each can reward theater. They may be reported only alongside delivery, attribution, and verification outcomes.

## Phase 0 — Observational MVP

Capability prerequisite (2026-07-19): Booster-managed automatic
`hard`/`lead`/`consilium_bio` Sol routes now require capability-aware worker
routing before they can contribute calibration evidence. The wrapper preserves
Sol as the preferred route, classifies only the canonical ChatGPT-account 400,
uses one bounded GPT-5.5 fallback with sanitized provenance, and retries Sol
after the negative-cache TTL. Explicit model requests never downgrade. This is
a Phase-0 measurement-integrity repair, not Phase-2 scheduling, and it does not
increase the clean sealed calibration count.

### Artifact contract

One independently testable, file-and-CLI shipment that adds a separate atomic slice ledger/event log, conservative git baseline attribution, typed advisory closure bound to exact hashes, deterministic backlog routing, and diagnostic telemetry. It must not change native Codex behavior or install hard WIP/spawn/wait gates.

### Deliverables

- Versioned `state/slice_ledger.json` schema with `run_id`, slice identity, artifact contract, allowed paths, lifecycle state, owner/session metadata, and typed terminal disposition.
- Atomic acquire/update/release operations, stale-owner detection, collision handling, and one documented recovery command.
- Append-only event log for activation, contract changes, ownership claims, worker attempts, observations, verification, closure, and recovery provenance.
- Git adapter capturing HEAD, tree, porcelain state, scoped hashes, baseline-dirty paths, untracked paths, and concurrent mismatches.
- Attribution engine producing candidate-owned, foreign, ambiguous, and off-scope classifications without automatic ambiguity resolution.
- Advisory closure: `committed | quarantined | delivered_uncommitted | blocked`, bound to the exact diff/tree hash and verification result.
- Append-only backlog entries for off-scope findings with source slice, reason, and provenance.
- Compact handoff receipt separating facts, claims, and unknowns.
- Versioned telemetry adapters for activation delay, first worker, first verification, first implementation commit, spawns, waits, progress events, dirty delta, commit classes, scope drift, parser coverage, and unknown rate.
- Fixtures and documentation for supported and unsupported control surfaces.

### Dependencies

- Existing autopilot goal contract and directional autopilot state remain separate and stable.
- Existing phase, evidence, verification, delegation, and `/go` surfaces are reused where contracts match.
- Git must be available; unsupported/non-git contexts fail explicitly rather than inventing a baseline.

### Acceptance tests and evidence

- Clean baseline activation and idempotent state updates.
- Baseline-dirty overlap is classified foreign/ambiguous and cannot enter a wrapper-managed commit.
- Relevant untracked paths are classified without recursively hashing unrelated large or secret trees.
- Two concurrent acquisitions fail closed; neither silently overwrites ownership.
- Crash/stale-owner recovery succeeds through one command and appends provenance instead of deleting history.
- Verification becomes stale when the diff/tree hash changes and closure is refused.
- Ledger `verified`/`committed` claims are rejected when git facts disagree.
- Partial quarantine preserves attributed paths and records all excluded paths.
- Off-scope path drift appends a backlog record; semantic drift remains advisory.
- Parser schema drift produces `unknown` plus reduced coverage, never zero-valued fiction.
- Native Codex activity outside a Booster wrapper is reported as diagnostic/unobserved, never controlled.
- Tests emit reproducible command output, exit status, and inspected hashes/trees as evidence.

### Exit criteria

- All Phase 0 contract tests pass.
- One recovery command handles every supported stale-lock fixture.
- No fixture permits foreign/baseline-dirty paths into a managed commit.
- Instrumentation is run across at least 10 real autopilot sessions.
- Calibration labels and raw receipts are sufficient to calculate every KPI marked for promotion.
- Overhead, repair rate, false-quarantine rate, parser coverage, and unknown rate are reported honestly.

### Rollback

Disable wrapper integration and retain the append-only event log plus git receipts for diagnosis. Directional autopilot state continues unchanged. Remove no history and auto-commit nothing during rollback.

## Phase 1 — Proven WIP=1 enforcement

### Promotion gate

Phase 1 may start only after >=10 measured sessions, attribution false-quarantine <15%, manual repair <20%, zero foreign-path managed commits, recoverable stale locks, and acceptable median overhead (<10% or a documented measured benefit).

### Artifact contract

Enforce one active implementation slice and closure-before-next-slice only at observable surfaces: Claude Code hooks and Booster-managed wrappers. Preserve an explicit bypass/recovery trail. Make no native Codex enforcement claim.

### Deliverables

- WIP=1 gate for supported hooks/wrappers.
- New-slice gate requiring current typed terminal disposition.
- Explicit, provenance-recorded contract expansion for legitimate dependency paths.
- Booster-worker attempt IDs, role, brief hash, retry number, evidence delta, and failure reason.
- Duplicate-brief advisory or gate, promoted only when calibration demonstrates acceptable false positives; override requires new evidence and provenance.
- `/start`, autopilot status, and handover summaries showing capability scope, KPI trend, coverage, and unknowns.

### Dependencies

- Stable Phase 0 schema and migration/version policy.
- Calibrated attribution review labels and trustworthy git adapters.
- Hook/wrapper capability detection so unsupported surfaces remain advisory.

### Acceptance tests and evidence

- Second slice is rejected while a supported-surface slice is active and unclosed.
- A valid terminal disposition permits the next slice.
- Wrapper bypass/recovery is explicit, append-only, and visible in receipts.
- Concurrent worktree changes force ambiguity/quarantine rather than false ownership.
- Native Codex calls remain possible and are labeled outside enforcement coverage.
- Controlled rollout shows no foreign-path commits and stays within KPI thresholds.

### Exit criteria

- WIP=1 operates successfully on supported surfaces through a defined evaluation window.
- Verified-delivery/domain-transition KPI reaches >=80%.
- Attributed-path closure coverage is 100%.
- No immediate kill criterion fires.

### Rollback

Switch WIP and next-slice gates to advisory mode while keeping Phase 0 measurement active. Preserve state and events; do not rewrite receipts or infer ownership retroactively.

## Phase 2 — Empirical wrapper-only orchestration decisions

### Promotion gate

Phase 2 requires stable wrapper event semantics, adequate parser/progress coverage, >=10 measured sessions, and an empirical relationship between orchestration behavior and verified terminal outcomes.

### Artifact contract

Add only Booster-wrapper decisions for spawn, retry, and wait behavior using observed progress events and empirical distributions. There is no fixed “three waits” rule and no native Codex interception.

### Deliverables

- Progress-aware wrapper policy using attempt identity, evidence delta, worker state, elapsed distributions, and terminal outcomes.
- Policy configuration derived from recorded percentiles/confidence bounds rather than arbitrary constants.
- Explicit interventions: continue waiting, consume partial result, stop worker, or retry with narrower/different decomposition.
- Shadow/advisory evaluation before any blocking policy.
- Per-policy outcome comparison covering completion, verified delivery, false interruption, retries, overhead, and ambiguity.

### Dependencies

- Phase 1-supported wrapper surfaces and Phase 0 event history.
- Sufficient verified terminal slices to avoid optimizing against a zero denominator.

### Acceptance tests and evidence

- Identical unchanged waits do not trigger a hardcoded count rule.
- Progress events reset/reframe intervention decisions according to documented policy.
- Native Codex waits remain diagnostic only.
- Shadow replay and controlled rollout demonstrate improved or non-inferior verified delivery without unacceptable false stops.
- Policy decisions are reproducible from recorded inputs and preserve provenance.

### Exit criteria

- A wrapper policy beats the observational baseline on verified terminal outcomes or materially reduces overhead/ambiguity without degrading delivery.
- False intervention and repair rates remain within approved thresholds established from calibration.
- Capability statements remain accurate for every surface.

### Rollback

Return orchestration policy to shadow/advisory mode. Keep WIP=1 only if its independent Phase 1 criteria still pass; otherwise roll it back separately. Retain event evidence for redesign.

## Implementation order

### Phase 0

- [x] Freeze and test the versioned ledger schema, lifecycle, terminal dispositions, and claim-versus-fact invariants.
- [x] Define event schema, append semantics, corruption behavior, and provenance requirements.
- [x] Implement atomic persistence, acquisition, collision handling, stale-owner detection, and one-command recovery.
- [x] Implement the git baseline adapter with scoped hashing and explicit unsupported states.
- [x] Implement conservative path attribution and artifact-contract expansion with reasons.
- [x] Implement exact-hash verification and typed advisory closure.
- [x] Implement append-only backlog routing and compact fact/claim/unknown handoff receipts.
- [x] Implement versioned telemetry adapters with coverage and `unknown` reporting.
- [x] Add clean, dirty, overlap, untracked, crash, concurrent, hash-change, quarantine, parser-drift, and native-Codex-boundary fixtures.
- [x] Integrate the MVP in advisory mode with Booster autopilot without merging directional and slice state.
- [ ] Instrument and review at least 10 real sessions.
- [ ] Calculate KPI bundle and write a promotion decision from evidence.

### Phase 1

- [ ] Confirm every Phase 1 promotion threshold from calibration data.
- [ ] Add capability-detected WIP=1 and closure-before-next-slice gates to Claude hooks and Booster wrappers.
- [ ] Add worker attempt/brief/retry provenance and evaluate duplicate-brief detection.
- [ ] Surface scoped efficiency status in `/start`, autopilot status, and handover.
- [ ] Run controlled evaluation and issue PASS/ROLLBACK decision against KPI and kill thresholds.

### Phase 2

- [ ] Build empirical spawn/wait distributions joined to verified terminal outcomes and progress coverage.
- [ ] Specify progress-aware decisions without a fixed wait count.
- [ ] Run shadow replay, then advisory live evaluation.
- [ ] Promote only a policy that improves outcomes without breaching false-intervention, overhead, or safety limits.

## Risks and controls

| Risk | Control |
|---|---|
| Ledger becomes stale authority | Git/filesystem facts always outrank claims; mismatch fails closed |
| Concurrent sessions overwrite ownership | Atomic acquisition, run/session identity, collision state, provenance-preserving recovery |
| Legitimate work is falsely quarantined | Conservative calibration labels, <15% promotion threshold, reasoned contract expansion |
| Foreign work is committed | Baseline dirt is foreign by default; exact staged-diff guard; immediate rollout kill on first event |
| Metrics reward ceremony | Bundle delivery, attribution, and verification outcomes; keep raw counts diagnostic |
| Parser/schema drift creates false certainty | Versioned adapters, fixtures, coverage ratio, explicit `unknown` |
| Controls consume too much time | Median overhead target <10%; rollback if no measured ambiguity/scope benefit |
| False parity between Claude and Codex | Capability detection and separate claims for hooks, wrappers, and post-hoc native Codex diagnostics |
| Semantic classifier blocks valid dependencies | Semantic drift stays advisory; deterministic paths and explicit contract expansion govern scope |
| Architecture expands prematurely | File/CLI-only design; no daemon, dashboard, database, service, or scheduler |

## Immediate kill criteria

Stop or revert enforcement immediately if:

- one foreign or baseline-dirty file enters an automatic/wrapper-managed commit;
- a stale ledger lock blocks valid work and one documented command cannot recover it;
- the ledger reports `verified` or `committed` against a non-matching diff/tree hash;
- one concurrent session silently overwrites ledger ownership; or
- post-hoc Codex observation is presented as native hard enforcement.

After an evaluation window, redesign instead of promoting if manual repair exceeds 20%, false quarantine exceeds 15%, or median overhead exceeds 10% without a measurable reduction in ambiguous attribution or scope breaches.

## Definition of Done

The roadmap is complete only when all of the following are true:

- Every supported Booster-managed slice begins with an artifact contract and observed git baseline.
- Every changed path at closure has an explicit, evidence-backed classification.
- Verification and terminal disposition are bound to the exact diff/tree hash.
- No foreign or baseline-dirty path has entered a wrapper-managed commit.
- Off-scope findings are preserved in an append-only backlog rather than silently becoming active work.
- At least 80% of measured sessions deliver a verified implementation commit before changing roadmap domain, or close with an explicit non-commit disposition.
- WIP=1 is enforced only on demonstrated observable surfaces and only after promotion thresholds pass.
- Any spawn/wait intervention is wrapper-only, progress-aware, empirically justified, reversible, and never a hardcoded three-wait rule.
- Native Codex limitations, parser coverage, and unknown states are visible and truthfully reported.
- Recovery is one-command, provenance-preserving, and tested; all immediate kill criteria have remained at zero during the final evaluation window.
- The solution remains a small file/CLI control plane with no daemon, dashboard, database, service, or scheduler.
