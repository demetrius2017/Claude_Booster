# GPT-6 Astra routing — /go Prototype Gate notebook

**Timestamp:** 2026-09-10T09:51:25Z
**Repo / environment:** `/Users/dmitrijnazarov/Projects/Claude_Booster`; Codex `codex-cli 0.154.0`
**Operation allowlist:** `CLI_READ`, `FILESYSTEM_READ` — allowed. No repository, real balancer JSON, or real memory DB was changed. No commit.

This is an investigation journal for the scoped GPT-6 Astra routing migration. It binds live lane-A invocation evidence to a disposable lane-B static/prototype check, then records the first concrete divergence from the present Sol/Terra/Luna system.

## Evidence cell L1 — live invocation 01

| Field | Evidence |
|---|---|
| Exact command / query | `codex exec --model gpt-6-astra --ephemeral --sandbox read-only --ignore-rules --json --color never "Reply with exactly ASTRA_OK:laneA_20260910_01 and nothing else."` |
| Source identity | Codex CLI, `codex-cli 0.154.0`; provider/model line absent |
| UTC / window | `2026-09-10T09:50:56Z–2026-09-10T09:51:11Z` |
| Baseline identity | Repository: `/Users/dmitrijnazarov/Projects/Claude_Booster`; raw gpt-5.6-sol identification: not run |
| Filters | `--model gpt-6-astra --ephemeral --sandbox read-only --ignore-rules --json --color never`; exact-output prompt |
| Expected / actual | `ASTRA_OK:laneA_20260910_01` → `ASTRA_OK:laneA_20260910_01` |
| Counts / sample | 1 invocation; exit `0` |
| Boolean invariant | **true** — exit was zero and result exactly matched the requested token |
| Warnings | service tier priority omitted; model metadata not found/fallback; Context7 auth transport error |
| SHA-256 command | `e4a6b46b4d883180d5d52b90e52c8b73d4f99ddbc931d42c3d7ce0c90468f879` |
| SHA-256 input / prompt | `61e7dec66e4ac8dffa5d28d2cb9194080e09be5f9d72bee5e1144111da78ffb2` |
| SHA-256 output / stdout | `eac4f14a48377dce5cc10ccf0bec616db06fd2e8e187d62dea622750f87c577b` |
| SHA-256 raw / stderr | `e87d7062b2cb5adea2b50f0b054dbba0d3c19e3f8def3030a9da2dcdeded4dc7` |

Raw transcript payload is unavailable: only the supplied stdout/stderr SHA-256 bindings were collected.

## Evidence cell L2 — live invocation 02

| Field | Evidence |
|---|---|
| Exact command / query | `codex exec --model gpt-6-astra --ephemeral --sandbox read-only --ignore-rules --json --color never "Reply with exactly ASTRA_OK:laneA_20260910_02 and nothing else."` |
| Source identity | Codex CLI, `codex-cli 0.154.0`; provider/model line absent |
| UTC / window | `2026-09-10T09:51:11Z–2026-09-10T09:51:25Z` |
| Baseline identity | Repository: `/Users/dmitrijnazarov/Projects/Claude_Booster`; raw gpt-5.6-sol identification: not run |
| Filters | `--model gpt-6-astra --ephemeral --sandbox read-only --ignore-rules --json --color never`; exact-output prompt |
| Expected / actual | `ASTRA_OK:laneA_20260910_02` → `ASTRA_OK:laneA_20260910_02` |
| Counts / sample | 1 invocation; exit `0` |
| Boolean invariant | **true** — exit was zero and result exactly matched the requested token |
| Warnings | service tier priority omitted; model metadata not found/fallback; Context7 auth transport error |
| SHA-256 command | `b3dd8fbb48cbba9e1d91a4658a9ad1fa4650688754e54cde4aece4f001848bad8` |
| SHA-256 input / prompt | `7883e2e6d18f34a70437f75240227ab2323b3b0c409ca151230fc84c0d661b32` |
| SHA-256 output / stdout | `60b6082636b30f5449b93a7148443faaae8fef51bb85230e61b0413bb966ed3d` |
| SHA-256 raw / stderr | `7ffaa6422b997a38c1dcbf480899279a5e57fb158c3bc6b200c1dd1c2bff3748` |

Raw transcript payload is unavailable: only the supplied stdout/stderr SHA-256 bindings were collected.

## Evidence cell S1 — canonical route normalization and persistence

| Field | Evidence |
|---|---|
| Exact command / query | `get_routing` → `{"model":"gpt-5.6-sol","provider":"codex-cli","reasoning_effort":"medium"}`; disposable execution in `/tmp/booster-lane-b.*` |
| Source identity | Lane B static evidence, 2026-09-10 UTC; canonical `consilium_bio` route |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | No repository, real balancer JSON, or real memory DB was changed |
| Filters | Canonical `consilium_bio`; extra fields; `get` and `decide` persistence paths |
| Expected / actual | `codex-cli:gpt-5.6-sol`, `medium` → exact; extras preserved and `xhigh` normalized to `medium`; read-only get unchanged; decide atomically persisted normalized route |
| Counts / sample | `get`: one before/after digest `3a9b…79e28`; `decide`: one new digest `bcda…7d94`; sample extras: `account`, `note` |
| Boolean invariant | **true** — normalization preserves unknown route extras, `get` does not persist, and `decide` persists atomically |
| SHA-256 command / input / output / raw | Unavailable: commands, inputs, outputs, and raw transcripts were not collected; abbreviated persistence digests only were supplied |

## Evidence cell S2 — active scorer and worker failure policy

| Field | Evidence |
|---|---|
| Exact command / query | Static prototype checks in `/tmp/booster-lane-b.*`; exact individual commands unavailable (not collected) |
| Source identity | Lane B static evidence, 2026-09-10 UTC; `model_balancer` active scorer and `codex_worker` wrapper policy |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | `consilium_bio` absent from `_PINNED_CATEGORIES`; current managed models are Sol/Terra/Luna |
| Filters | Unpinned active scorer; canonical entitlement JSON failure versus metadata-only warning; explicit Sol source |
| Expected / actual | Sol can be resurrected when samples win → confirmed: five Sol samples at 10ms versus five Terra samples at 1000ms selected Sol. Worker fallback only for managed Sol plus exact entitlement JSON retrying Terra once → confirmed. |
| Counts / sample | Scorer: 5 Sol + 5 Terra samples. Canonical failure: `rc=0`, calls `[Sol, Terra]`, effective Terra. Metadata-only warning: `rc=1`, calls `[Sol]`, no fallback. Explicit Sol: `rc=1`, calls `[Sol]`, source `explicit`. |
| Boolean invariant | **true** — fallback is constrained to managed Sol and exact entitlement JSON; unpinned scorer can select Sol |
| SHA-256 command / input / output / raw | Unavailable: no hashes for these static probe transcripts were collected |

## Evidence cell S3 — telemetry, cache identity, and source scan

| Field | Evidence |
|---|---|
| Exact command / query | Static prototype checks and source scan in `/tmp/booster-lane-b.*`; exact individual commands unavailable (not collected) |
| Source identity | Lane B static evidence, 2026-09-10 UTC; `model_metric_capture` and capability-cache schema |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | Current telemetry recognizes Sol/Terra attempts; exact cache schema: `schema_version, model, reason, observed_at, expires_at` |
| Filters | Valid telemetry trailer; Astra input; source scan of three target scripts/tests; cache extra-key rejection |
| Expected / actual | Sol/Terra wrapper attempts recorded; Astra support absent → Sol failed/11ms and Terra success/12ms inserted, Astra input returned `False`, and source scan found **0** Astra references. Cache has no version/account fingerprint; extra keys fail closed. |
| Counts / sample | 2 telemetry rows; 0 Astra references; 5 cache schema keys |
| Boolean invariant | **true** — present telemetry allowlist excludes Astra and cache rejects schema expansion without an explicit versioned change |
| SHA-256 command / input / output / raw | Unavailable: no hashes for these static probe transcripts were collected |

## Evidence cell S4 — installer safety and installed-parity precondition

| Field | Evidence |
|---|---|
| Exact command / query | `python3 tests/test_gpt56_routing.py` |
| Source identity | Repository test suite; Lane B static evidence, 2026-09-10 UTC |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | Source `install.py` SHA-256: `ee0807aadbf48f7c1e33b3929952c914cff3a95e95b64066e9446aaea9bf6e85` |
| Filters | GPT-5.6 routes and effort contracts |
| Expected / actual | PASS → `PASS: GPT-5.6 routes and effort contracts` |
| Counts / sample | Pass count not separately emitted; sample is the suite success line |
| Boolean invariant | **true** — existing canonical routing/effort contract suite passed |
| SHA-256 command / input / output / raw | Unavailable: no hashes for this test execution were collected |

| Field | Evidence |
|---|---|
| Exact command / query | `pytest -q tests/test_codex_capability_routing.py tests/test_codex_adapter_exact_route_contract.py` |
| Source identity | Repository test suite; Lane B static evidence, 2026-09-10 UTC |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | Source snapshots bound below |
| Filters | Capability routing and exact adapter route contract |
| Expected / actual | pass → `26 passed in 4.19s` |
| Counts / sample | 26 passed; 4.19s |
| Boolean invariant | **true** — current capability routing and adapter route contract passed |
| SHA-256 command / input / output / raw | Unavailable: no hashes for this test execution were collected |

| Field | Evidence |
|---|---|
| Exact command / query | `PYTHONPATH=/Users/dmitrijnazarov/Projects/Claude_Booster pytest -q tests/test_install_codex_bridge_safety.py` |
| Source identity | Repository installer safety suite; Lane B static evidence, 2026-09-10 UTC |
| UTC / window | 2026-09-10 UTC; exact sub-window unavailable (not collected) |
| Baseline identity | `install.py` SHA-256: `ee0807aadbf48f7c1e33b3929952c914cff3a95e95b64066e9446aaea9bf6e85` |
| Filters | Installer atomic-write, hash-verification, and rollback safety |
| Expected / actual | pass → `6 passed in 1.12s` |
| Counts / sample | 6 passed; 1.12s |
| Boolean invariant | **true** — canonical installer safety suite passed with repository root on `PYTHONPATH` |
| SHA-256 command / input / output / raw | Unavailable: no hashes for this test execution were collected |

The first installer-suite invocation diverged only because pytest lacked the repository root on its import path (`ModuleNotFoundError: install`); with `PYTHONPATH` set, all six tests passed. The installer’s relevant behavior is `atomic_write` using fsync plus `os.replace`; the bridge backs up changed files, hash-verifies, and restores backups or removes created files on exception.

## Prototype verdict: PASS

The live CLI accepted `gpt-6-astra` for two independently token-bound read-only invocations. The static prototype proves the present repository does not yet route, manage, or record Astra. This is a pass for the Prototype Gate because the migration boundary and fail-loud constraints are now concrete; it is not evidence that the repository implementation already supports Astra.

## Artifacts

- This notebook: `reports/prototypes/gpt6_astra_routing_2026-09-10.md`
- Live lane-A evidence: exactly two supplied `codex exec` invocations, cells L1 and L2.
- Static lane-B evidence: disposable-only checks under `/tmp/booster-lane-b.*`, cells S1–S4.

## Baseline/source snapshot binding

| Source | SHA-256 |
|---|---|
| `model_balancer.py` | `1432983d3d0939133b9076aff11f93aa67f39fccbe19f221a2f95865031f34c3` |
| `codex_worker.py` | `6690436d6a3496f7e5b4cf338dd9af0f3c03c3a22a047b92c1a509232b2f8790` |
| `model_metric_capture.py` | `8aebe4e2b67070ea9ab409ab37a120fd167f330f4451eba786a3920f96764616` |
| `install.py` | `ee0807aadbf48f7c1e33b3929952c914cff3a95e95b64066e9446aaea9bf6e85` |

No additional source hashes are invented in this notebook.

## Source-of-truth inputs

1. The two exact live commands, UTC windows, outputs, warnings, and SHA-256 values supplied under LIVE EVIDENCE.
2. The Lane B static evidence supplied under STATIC EVIDENCE, including the canonical route result, current behavior samples, test commands, results, and source snapshot hashes.
3. The supplied scope constraints: no repository, real balancer JSON, or real memory DB mutation; no commit.

## Current-system comparison

| Boundary | Current system | Astra migration requirement |
|---|---|---|
| Default route / normalization | Canonical `consilium_bio` is `codex-cli:gpt-5.6-sol`, `medium`; extras survive normalization | Add a provider-qualified Astra route through the documented canonical route shape, without silently accepting malformed custom shape |
| Active scorer | Unpinned `consilium_bio` can resurrect Sol when its samples win | Decide and document the intended Astra eligibility/pinning behavior rather than inheriting it accidentally |
| Worker | Managed Sol has narrowly constrained entitlement fallback to Terra | Update managed-model recognition and canonical failure policy for Astra together with the route change |
| Telemetry | Sol/Terra wrapper attempts are recognized; Astra is rejected | Add Astra allowlist/provenance validation together with worker support |
| Capability cache | Schema has five canonical keys and fails closed on extras | Leave capability-context fingerprinting out of this scoped migration; version schema separately if that hardening is adopted |
| Installer | Per-file atomic write plus backup/hash/rollback transaction | Do not redesign it; verify installed parity after the canonical migration |

## First divergence

There is no “Astra” model in the three target scripts or tests. The current system is explicitly Sol/Terra/Luna; therefore neither worker management nor telemetry recognition of Astra exists. This is the first divergence between successful live CLI acceptance and repository-level routing support.

## Counts and samples

- Live invocations: exactly **2**; both exit `0`; both exact-token invariants true.
- Raw gpt-5.6-sol identification invocations: **0** (not run).
- Astra references in the three target scripts/tests: **0**.
- Active scorer sample: **5** Sol samples at 10ms versus **5** Terra samples at 1000ms; Sol selected.
- Worker samples: canonical managed-Sol entitlement failure → two calls `[Sol, Terra]`; metadata-only warning → one `[Sol]`; explicit Sol → one `[Sol]`.
- Telemetry sample: **2** rows, Sol failed/11ms and Terra success/12ms; Astra input returned `False`.
- Tests: GPT-5.6 route suite passed; capability/adapter suites **26 passed in 4.19s**; installer safety suite **6 passed in 1.12s** after setting `PYTHONPATH`.

## Invariants proven

1. Both supplied live Astra invocations returned their requested exact sentinel with exit `0`.
2. Canonical route normalization preserves extra fields, normalizes unsupported effort to `medium`, keeps `get` read-only, and atomically persists `decide`.
3. The active scorer may select unpinned Sol from superior samples; existing route category behavior must therefore be made intentional.
4. Existing worker fallback is fail-closed: only managed Sol plus the exact canonical entitlement JSON retries Terra; warnings and explicit model selections do not broaden fallback.
5. Current telemetry records Sol/Terra wrapper attempts but rejects Astra, while the current cache schema fails closed on unexpected keys.
6. The canonical installer’s per-file atomic write plus backup/hash/rollback behavior is sufficient for this file-level migration; installed parity remains a deployment-gate obligation.

## Challenge additions disposition

- **Next-day scorer gap — PROVEN and must be fixed.** The unpinned category’s active scorer can resurrect Sol from historical winning samples. The implementation must explicitly define the canonical Astra route category behavior and test that next-day selection cannot silently regress it.
- **Canonical-vs-custom migration ambiguity — must be resolved.** Document and implement one provider-qualified canonical route shape. Custom or malformed route representations must not become an alternate implicit contract.
- **Malformed route behavior — preserved fail-loud.** Preserve rejection of invalid/unknown schema material and do not add permissive fallback parsing.
- **Cache capability-context fingerprint — NOT REQUIRED for this scoped migration.** Binding by account or Codex version is scope-expanding hardening because it requires a cache schema/version and test change.
- **Installer multi-file transaction — NOT REQUIRED.** The canonical installer already has per-file atomic write plus backup rollback. This migration still requires installed-parity verification, rather than an installer transaction redesign.

## Worker handoff

Implement the canonical Astra migration as one coupled change: update `model_balancer`, `codex_worker`, and `model_metric_capture` together. Provider-qualified documentation must name the canonical route shape and distinguish it from malformed/custom input. Add durable tests only at the final deploy gate, including the next-day scorer regression, malformed-route fail-loud behavior, worker canonical-failure boundary, and telemetry/provenance recognition.

Before delivery, perform installed migration plus a live routed smoke that proves the installed bridge preserves source behavior. Preserve PAL Sol and all untracked wait-history: neither may be deleted, normalized away, or treated as migration noise.
