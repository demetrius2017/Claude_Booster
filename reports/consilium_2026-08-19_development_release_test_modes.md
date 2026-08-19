# Consilium — Development/Release Test Modes

**Date:** 2026-08-19
**External independence:** degraded. PAL GPT-5.5 returned HTTP 429 credit exhaustion; GLM-5.2 via Z.ai returned HTTP 429 insufficient balance; Grok reached max turns without an opinion. The fifth usable view is a same-provider Codex second opinion.

## Task context

Proposal: during development run impacted tests, critical smoke, unresolved prior failures, and a deterministic stratified 10% sample of unaffected tests. At release, freeze the test contract and run the full suite.

## Verified Facts Brief

- /go, paired verification, and hackathon already prohibit creating or rewriting test artifacts during implementation and defer durable regression tests to the final gate (templates/commands/go.md:688; templates/rules/paired-verification.md:241; templates/commands/hackathon.md:100).
- Their final gates freeze regression manifests/test hashes and run the full existing suite (templates/commands/go.md:943; templates/rules/paired-verification.md:252; templates/commands/hackathon.md:109).
- Generic phase guidance still says IMPLEMENT: run tests after each change without limiting scope (templates/scripts/phase.py:32; templates/commands/phase.md:24).
- No executable mode dispatcher, impact map, critical-smoke registry, failure ledger, or sampler exists.
- Tests span tests/, templates/scripts/tests/, and templates/scripts/supervisor/tests/; broad RECON found about 134 test files/scripts.
- At synthesis the repository was locally on main but phase PLAN. Therefore local branch main cannot itself imply release without disabling the proposed acceleration in the actual workflow.
- Phase is stored in mutable local file .claude/.phase. It expresses local intent but is not a protected CI trust boundary.

## Agent positions

| Agent | Position | Key insight | KPI |
|---|---|---|---|
| Test-pipeline architect | FOR with conditions | One central dispatcher; ambiguity and mapping failures expand to release; emit an auditable decision artifact. | Development p50 no more than 20% of full suite |
| Reliability engineer | FOR with fail-closed selector | Selective execution improves feedback but cannot be release evidence; unresolved failures stay selected until valid closure. | Critical and prior-failure inclusion 100% |
| DX/performance engineer | FOR | One simple command and shadow rollout; agents must not launch broad suites ad hoc during IMPLEMENT. | 60–75% time-to-signal reduction |
| Adversarial safety reviewer | FOR architecture, AGAINST policy-only rollout | Bind release receipt to exact merge-result tree/SHA; defend against phase laundering, skips, stale caches, and CI bypass. | Every promotion receipt matches promoted tree |
| Codex second opinion | FOR; degraded independence | Ten percent is an exploration budget, not statistical safety evidence. | Track selector false negatives |

## Decision

Adopt two executable modes. One central dispatcher switches them; neither the user nor an agent chooses ad hoc.

### Mode resolution

1. Trusted CI merge-queue/protected-branch event bound to the exact merge-result SHA: **release**.
2. Fresh local phase lease VERIFY or MERGE: **release**.
3. Explicit mode release: **release**; a manual action may raise strictness.
4. Fresh local phase lease PLAN, IMPLEMENT, or AUDIT: **development**, even if the local branch is named main.
5. Missing, malformed, expired, conflicting, or unverifiable phase state; unknown CI context; selector or manifest failure: **release**.

Branch name is telemetry only. It cannot downgrade release. Protected CI metadata overrides all local state. Explicit development cannot override release conditions.

The phase marker needs a lease containing phase, creation time, session/run identity, and project-root binding. Old IMPLEMENT state from a prior session must expire.

### Development selection

Run all conservatively impacted tests, all versioned critical-smoke tests, all unresolved prior failures, and a deterministic stratified sample of unaffected tests.

Ten percent is the initial tunable cost budget. It is not coverage proof. Sampling is reproducible from immutable inputs such as base SHA, candidate tree SHA, registry hash, and sampling epoch. Critical tests are never sampled.

Unknown changed paths, dynamic/generated dependency ambiguity, selector changes, or an implausibly empty impacted set expand scope or fall back to release.

### Release gate

The release gate recomputes the registry from the exact candidate tree; freezes the manifest, tests, selector, runner, workflow/config, lockfiles, and runtime identity; runs the full suite; and emits a receipt bound to commit SHA and tree SHA. Evidence from another SHA, skipped required jobs, continue-on-error, or stale artifacts cannot admit promotion.

Selective development PASS is advisory feedback. Only the exact-tree full-suite release receipt admits promotion.

## Minimum architecture

1. Central test dispatcher with JSON decision receipt.
2. Versioned test registry with stable IDs, commands, components, criticality, expected duration, and platform.
3. Versioned impact map from sources/components to conservative test IDs.
4. Critical-smoke registry with owner, rationale, and review date.
5. Append-only failure ledger binding test identity/content hash, candidate, failure signature, and resolution.
6. Expiring project/run-bound phase lease.
7. Frozen exact-tree release receipt.

## Rollout

1. **Shadow:** compute development plans while still running the full suite; measure omissions and duration.
2. **Development enforcement:** replace broad intermediate suites only after mapping and false-negative targets pass.
3. **Protected release enforcement:** CI requires an exact merge-result full-suite receipt.

Start with explicit high-value mappings. Do not infer the universal impact map live with an LLM.

## KPIs

- Development p50 wall-clock at most 25%, p90 at most 45% of full suite.
- Critical-smoke and unresolved-prior-failure inclusion: 100%.
- Release conditions executing full suite: 100%.
- Receipt SHA/tree matching promoted SHA/tree: 100%.
- Deterministic rerun equivalence: 100%.
- Critical regressions omitted by development selection: 0.
- Non-critical selector false-negative rate: measure in shadow; target below 0.5% after calibration.
- Mapping coverage: at least 80% before enforcement and 95% steady state.
- Dispatcher planning latency below 1 second cold.
- Duplicate development executions on unchanged candidate below 5%.

## Strongest counterargument

An impact map is least trustworthy during initial rollout. A 10% sample misses one omitted failing test with 90% probability, and correlated failures invalidate naive Monte Carlo confidence. The sample is therefore bounded exploration, never a replacement for final full-suite verification.

## Rejected alternatives

- Branch dev as authoritative marker: unreliable under worktrees, detached heads, rebases, local-main development, and merge queues.
- Local main always means release: contradicted by the observed local main plus PLAN workflow and would remove the speedup.
- Pure random 10% of all tests: can omit impacted/critical tests and is irreproducible.
- Prose-only changes: current /go prose already contains much of the separation, yet ordinary runs remain uncontrolled.
- Replacing final full suite with accumulated samples: samples are not equivalent to one frozen exact-tree run.

## Risks and controls

- Stale phase laundering: expiring project/run-bound lease; unknown is release.
- Selector blind spots: conservative mapping; unknown expands scope; release-only misses create mapping debt.
- Failure laundering by rename/delete/skip/xfail: ledger binds test identity and content hash; such changes trigger release.
- Flakes becoming permanent tax: evidence-based classification and expiry-bound quarantine, never silent deletion.
- Cache lies: exact fingerprints only; release ignores development cache.
- Agent bypass: during IMPLEMENT broad raw suite commands should yield to dispatcher-selected tests and required direct probes.

## Final verdict

**APPROVE as an executable, fail-closed two-mode architecture.**

Development optimizes time-to-signal. Release protects truth. The dispatcher switches modes; trusted CI controls promotion, while a fresh local phase lease controls development intent. Ten percent is a tunable exploration budget, not a safety guarantee.
