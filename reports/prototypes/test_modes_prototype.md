# Test modes: prototype investigation journal

**Prototype verdict: PASS.** The required implementation facts are now established, including two fail-closed constraints: the current phase marker cannot authorize a development mode lease, and the current dirty worktree cannot receive a promotion receipt. This is a journal, not an implementation or a synthetic application test.

## Source identity and observation environment

Observed at `2026-08-19T13:25:12Z` (UTC), with macOS 26.5.2 (25F84), Python 3.11.10, and Git 2.50.1 (Apple Git-155). Repository root and Git common directory were `/Users/dmitrijnazarov/Projects/Claude_Booster` and `.git`.

| Input | Identity at observation |
|---|---|
| `HEAD` / `HEAD^{tree}` | `ee870e62a362dee24b8a20d223adbdae33757886` / `b3ccf7fce5a69c1599823d231945d142193080ce` |
| index stage stream SHA-256 | `46300144af9b7516fc9287862082e2e0fc8a69e230e2a9278079da2ad8fc75e6` |
| `docs/test_modes_session_spec.md` blob | `e345e72a08cdbbb1b7562e928174ce934da6c888` |
| `templates/scripts/phase.py` blob | `c6a0b7b5f8107006fe19775e7f988fe18d2af128` |
| `slice_git_core.py` / `slice_ledger_core.py` blobs | `28172b7377398d4b6dff0fa54c2956ea3f63a10b` / `3a89d6242d4f16934da94c5615bcae70b3d4ad72` |
| `install.py` blob | `bc6805759eb420547a77bcd27f23411420bf3c9b` |

All project probes were read-only. The Git protocol experiment deliberately mutated only disposable repositories beneath `/private/tmp/test-modes-git-probe-6tvs2mu3`; no project hook, remote, config, tracked file, test, or state was modified. That operation is explicitly allowed by the prototype contract.

## Probe receipts

Command hashes are SHA-256 of the exact normalized command text below; result hashes bind the bounded raw evidence files.

| ID | Exact command / operation class | Allowlist decision | Expected → actual / invariant | Result and raw output |
|---|---|---|---|---|
| C1 `7cb31db7…e2615d` | `git rev-parse --show-toplevel --git-common-dir HEAD 'HEAD^{tree}'; git ls-files --stage -z \| shasum -a 256; git status --porcelain=v2 --untracked-files=all --ignored=matching; git diff --name-only; git diff --cached --name-only` — read-only Git | ALLOW | Clean candidate required for exact promotion → 2 tracked modifications and 11 untracked paths; invariant `clean_worktree == false` | SHA-256 `d3121898f3e98b60cd45de8c1fc8ab8c4fec78e27fd569df75ba7534ae280eb3`, [raw C1](test_modes_evidence/01_project_identity.txt) |
| C2 `62addc56…91516b` | Inline Python stdlib disposable-Git driver: bare remote + local work repo; captures pre-push records for update/tag/create/delete/force; separately sets `core.hooksPath` unset/relative/absolute and uses `lstat`/`readlink` only for a symlink target — isolated temporary mutation | ALLOW only for `/private/tmp`; project/external state fail-closed | Four protocol shapes observed; force shares update record shape; hook path is redirectable; invariant `project_git_state_unchanged == true` | SHA-256 `b7928aff516e46e31b3ebf79368ffa3ccccafe9315c6b1e14a0619a76f77ed43`, [raw C2](test_modes_evidence/02_git_protocol.txt) |
| C3 `91636738…e6cdc2` | `sed -n '1,260p' templates/scripts/phase.py; rg -l --hidden --glob '!.git/**' --glob '!reports/**' --glob '!docs/**' 'phase\\.py|\\.claude/\\.phase|\\.phase' . \| sort` — source/read-only search | ALLOW | Lease needs expiry/binding/atomicity → marker is a single phase token; 29 direct consumers found | SHA-256 `9e0623768ae39986886022a1844ed003b7c799dbb25c9a3616ea7dd63acd081d`, [raw C3](test_modes_evidence/03_source_inventory.txt) |
| C4 `9fa7078d…b7b66f` | `find tests templates/scripts templates/commands scripts -type f -name 'test_*' \| sort; rg -n --glob 'test_*' 'unittest|pytest|sqlite3|git clone|\\$HOME/\\.claude|timeout|mktemp' tests templates/scripts templates/commands scripts/codex_wait_patch` — inventory/read-only search | ALLOW | pytest-only bootstrap would be insufficient → 37 Python plus 47 shell tests; Python has pytest and unittest; shell tests include temp, HOME/SQLite, and clone cases | raw C3, same source scan |
| C5 `4fc3f86a…620b45` | `sed -n '1,430p' templates/scripts/slice_git_core.py; sed -n '1,470p' templates/scripts/slice_ledger_core.py; rg -n 'atomic_write|pre-push|hooksPath|git/hooks|core\\.hooksPath|write_settings|write_all' install.py` — source/read-only search | ALLOW | Reuse secure primitives where their contracts fit → Git snapshot and ledger durability helpers exist; no dispatcher/lease/pre-push management does | raw C3, same source scan |
| C6 `605964a7…0c8003` | `find .github -maxdepth 3 -type f -print; git status --porcelain=v2 --branch; git log --oneline --decorate -12; git remote -v` — filesystem/Git read-only | ALLOW | Protected CI evidence must exist in repo → `.github` absent; current `main` is one commit ahead of `origin/main` | raw C1 and C3 |

The ellipsis in command hashes is only typographic; the full digest is retained in the source transcript for this session. C2's inline driver is fully characterized in raw C2 by its isolated paths, operations, and outputs; it has no repository dependency and writes no durable project artifact.

## 1. Phase contract and legacy marker

`phase.py` currently supports only `get` (or no argument), `set NAME`, and `list`. `VALID` is `RECON, PLAN, IMPLEMENT, AUDIT, VERIFY, MERGE`; missing, unreadable, malformed, or unknown marker returns `RECON`. `.claude/.phase` is exactly `PHASE + "\n"`. `set_phase` uses direct `write_text`, then best-effort appends a UTC line to `phase_transitions.log`; it has no lock, atomic replacement, schema version, `created_at`, `expires_at`, session/run ID, canonical-root binding, issuer metadata, or validation command.

This is the first decisive divergence from FR-3: the legacy marker records mutable local intent, not a lease. It has no lease at all, therefore it must never grant development mode by itself. Compatibility may read it only as legacy telemetry or as an explicit migration input; missing/malformed/conflicting legacy state must resolve to release.

The 29 direct consumers listed in raw C3 cover prompt/status presentation, phase/go/delegation gates, command templates, and acceptance tests. In particular, `go_gate.py`, `delegate_gate.py`, `phase_gate.py`, `phase_prompt_inject.py`, `preserve_plan_context.py`, and `statusline.sh` parse the marker independently. The command templates also invoke a nonexistent `phase.py progress`, a separate compatibility defect to resolve before making a lease API authoritative.

## 2. Candidate identity and receipts

The project was on `main`, tracking `origin/main`, one commit ahead. The stable committed anchors are `HEAD=ee870e6…`, tree `b3ccf7f…`, and index stream SHA-256 `46300144…`; however, status also reports two tracked modified files (`templates/commands/gantt.md`, `tests/test_gantt_contract.py`) and 11 untracked paths. `git diff --cached --name-only` was empty.

Expected promotion rule: an exact-tree receipt denotes one immutable candidate. Actual state: the working tree contains content not represented by `HEAD` or index identity. Boolean invariant: **FAIL** for `receipt_candidate_is_exact == (worktree_clean && receipt.commit == candidate.commit && receipt.tree == candidate.tree)`. Therefore a dirty candidate cannot produce a promotion receipt: a receipt for HEAD/tree is stale relative to working content, while a receipt that hashes mutable worktree content has no pushed commit identity. The dispatcher may still construct an advisory development plan, but it must mark it non-promotable and never mint a release/promotion receipt.

## 3. Pre-push and hook facts

The disposable probe observed these exact pre-push input forms:

| Operation | Local ref/OID | Remote ref/OID | Consequence |
|---|---|---|---|
| ordinary update | `refs/heads/main`, non-zero | `refs/heads/main`, non-zero | ancestry check is required to distinguish fast-forward |
| new annotated tag | `refs/tags/v-probe`, non-zero | same tag, all-zero OID | tag is a distinct ref class |
| new branch | `refs/heads/delete-me`, non-zero | same branch, all-zero OID | destination did not exist |
| deletion | `(delete)`, all-zero OID | `refs/heads/delete-me`, non-zero | never a promotion candidate |
| `--force` non-fast-forward | ordinary non-zero main OIDs | ordinary non-zero main OIDs | wire shape alone cannot identify force; test ancestry/result |

The resolver must parse every stdin record, reject zero local OIDs/deletions for promotion, classify tag/branch namespace explicitly, and query ancestry or protected-destination policy. It must not infer "safe" from a record shape.

`core.hooksPath` was unset in the project. In disposable repos: unset used default `.git/hooks`; relative `custom-hooks` executed its safe hook relative to that repo; absolute path executed its safe hook. The observed symlink was a link to `/bin/false`, inspected only by `lstat`/`readlink`. Implementation implication: install must preserve the effective user hook path, never blindly replace a user hook, reject unsafe symlink/hardlink/non-regular hook artifacts, and chain only after an owned hook/adapter is securely installed. Current `install.py` has no Git hook management at all.

## 4. Test execution bootstrap registry

The current suite is heterogeneous. A concrete bootstrap registry should use one versioned JSON/YAML record per managed command, with at least:

```text
id, runner_kind, command_argv, cwd, source_files, test_files, components,
test_class, platform, network_policy, state_scope, timeout_seconds,
critical, expected_duration_seconds, enabled, content_hash
```

Initial static categories are:

| Command family | Count/sample | Platform/network/state classification | Bootstrap runner |
|---|---:|---|---|
| `python3 -m pytest …` | pytest-only slice-calibration family | local Python; temporary-file fixtures; no network expected | argv runner, default timeout 120 s |
| `python3 -m unittest discover …` | supervisor suite; individual `unittest` modules | local Python, some SQLite/temp state | argv runner, default timeout 120 s |
| direct Python test files | root contract/acceptance suites | mixed: imports/templates; some SQLite/temp repos | argv runner, default timeout 120 s |
| Bash contract tests | 47 shell tests | mixed; several use `$HOME/.claude`, SQLite, temporary repos; `codex_wait_patch` tooling has `git clone` | `bash` runner; explicit network=`forbidden/isolated/required`; timeout per registry |

The registry must be bootstrap-generated from this inventory and then reviewed, not guessed from filename. Every command runs with explicit argv/cwd/environment allowlist and a timeout; tests whose contract writes user-home state require `state_scope=user-home-isolated` and cannot run as an unqualified parallel batch. There is currently no `pyproject.toml`, `pytest.ini`, `tox.ini`, or `package.json` identifying one universal runner.

## 5. Reusable state primitives versus gaps

`slice_git_core.py` provides reusable, evidence-grade primitives: `canonical_path`, `parse_porcelain_v2`, `stable_git_state`, `file_fact`, `snapshot`, `facts_compatible`, `classify`, and `attribution_receipt`. The useful properties are deterministic environment-limited Git calls, twice-sampled anchors, porcelain-v2 parsing, no-follow bounded file hashing, case/Unicode collision rejection, and `HEAD/tree/ref/index` anchoring. Reuse these for candidate snapshots and allowed-path attribution.

Do not stretch them beyond contract: they do not resolve push destinations or protected policy, make a worktree candidate commit, freeze test manifests, validate registries, select tests, execute commands, or issue promotion receipts.

`slice_ledger_core.py` contributes secure no-follow reads, canonical JSON SHA-256 hash-chain validation, append+`fsync`, and atomic projection with directory `fsync`. It is an internal slice-ledger implementation with private functions; the new receipt/lease subsystem should either expose a deliberately small common durability module or duplicate the security invariants with focused tests. It must not import private functions as an accidental API.

`install.py` already has a suitable `atomic_write` (temp file, fsync, Darwin `F_FULLFSYNC` when available, chmod, replace), managed ownership manifests, backups, and settings hook merge rules. It currently manages Claude settings hooks only: no `pre-push`, `hooksPath`, `.git/hooks`, or `core.hooksPath` code exists. Git-hook ownership and chaining are a new design surface, not an installer extension already proven by existing behavior.

## 6. Registry self-modification and CI trust

The current candidate is one commit beyond `origin/main`, while its working tree is dirty. Neither local branch name nor current history establishes a protected CI result. The registry self-modification threat is real conceptually: a candidate could change the impact/critical registry so that its own changed test is not selected.

**Trusted-base union rule:** for every candidate that changes any registry, selector, runner, test-contract, workflow/config, or lockfile input, compute required tests from the union of the trusted-base interpretation and the candidate-tree interpretation:

```text
required = critical(base) ∪ critical(candidate)
         ∪ impacted(base_registry, base→candidate)
         ∪ impacted(candidate_registry, base→candidate)
         ∪ unresolved_failures(base_ledger, candidate_ledger)
```

All registry/selector/config inputs themselves are forced-impacting and receipt-bound. If either side is missing, invalid, unavailable, or disagrees on stable test identity, resolve release and require the frozen full registry; no candidate may reduce its own required set. The trusted base must be the exact configured protected/merge base supplied by the CI adapter, never merely a local branch name.

No `.github` directory or workflow files were found. Thus live protected-CI enforcement is not observable in this project now. A trusted-CI adapter can be implemented and unit/integration-tested against signed/explicit fixture inputs, but only that adapter behavior—not live protection enforcement—can be proven locally.

## Prototype handoff

### Worker facts (may rely on)

- Legacy `.phase` has no lease fields and must not authorize development.
- The phase consumer set is 29 direct files and requires a compatibility migration plan.
- Dirty worktrees cannot yield promotion receipts.
- Pre-push deletion is a zero-local-OID record; update and force require ancestry/policy beyond record shape.
- `core.hooksPath` changes which hook Git executes.
- Test execution needs a multi-runner registry.
- Existing secure primitives provide snapshots and durable append/projection mechanics, not the new policy layer.
- No in-repo CI workflow proves protected-branch enforcement.

### Forbidden assumptions

- `main` means release; a legacy marker means fresh; `git push` means protected destination; pre-push cannot be bypassed; a force push has a unique wire shape.
- All tests are pytest, parallel-safe, offline, hermetic, or free of `$HOME/.claude` state.
- A candidate-modified registry can be trusted to decide its own coverage.
- `install.py` already owns Git hooks.
- A local adapter test proves hosted branch protection.

### Exact verifier probes after implementation

1. In a disposable repo, generate valid/malformed/stale/root-mismatched leases and assert JSON decision `release`; create a fresh root-bound IMPLEMENT lease and assert `development` only with no higher-trust context.
2. Build clean commits plus dirty tracked/untracked variants; assert promotion receipt issuance rejects all dirty candidates and accepts only exact matching commit/tree.
3. Feed pre-push stdin fixtures for branch update, tag, deletion, and force ancestry; assert protected destination or unknown policy resolves release, deletion has no receipt, and `--no-verify` has no CI bypass in adapter verification.
4. Set unset/relative/absolute `core.hooksPath` and regular/symlink user hooks in disposable repos; assert non-destructive chain/install policy and no execution of unsafe targets.
5. Generate the bootstrap registry, execute every currently registered command in its declared isolation mode, and fail on unknown framework/platform/network/timeout classification.
6. Run base-versus-candidate registry mutation fixtures; assert the trusted-base union includes tests removed by the candidate registry.
7. Freeze a release manifest before execution, mutate a test/registry/config afterward, and assert receipt/promotion fails; rerun only from the exact candidate tree.
8. Exercise the CI adapter with explicit merge-result SHA X and receipt SHA/tree Y; assert mismatch fails. Mark live protected enforcement `N/A` until a real workflow and protected-repository evidence exists.

## First divergence summary

The first implementation divergence is not test selection. It is trust representation: the requested mode rules depend on an expiring, root- and run-bound lease plus exact candidate identity, while the existing system has a mutable one-line phase marker and local Git state. Implement and migrate that trust boundary before allowing any development-mode shortcut.
