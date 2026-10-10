# Scoped project-contract successor

Status: implemented candidate; actual Horizon admission is not activated. Independent acceptance is recorded by the orchestrator, not asserted by this implementer.
Source baseline: `ca0b3662b6a162a1054a2928d179638153c0a7de`.
Horizon comparison baseline remains `dfc5536a`; this work does not replace it with HEAD.

## Contract and trust boundary

Fix project portability and support a first locally admitted baseline without mutating installed defaults or claiming pre-existing trust. Explicit operator-supplied `--admission` and `--admission-sha256` are required on both plan and run. The artifact must reside outside candidate root and Git common directory, be a regular non-link mode-0600 file owned by the effective user, and bind exact repository root/common-directory, original baseline commit, full registry, author, and independently produced review evidence. Review binds the admission subject digest and has a distinct reviewer label. Artifact and review bytes are pinned and revalidated at execution.

**This is an operator assertion, not cryptographic authentication of owner or reviewer identity.** Mode, hashes, and different identity labels prevent accidental substitution; they do not prove authority or independence. A separate independent review and authorized operator supply the real evidence and pin. This implementation never generates or activates admission. No Horizon artifact is created here.

Acceptance: project contracts take precedence; an external project without contracts fails closed; existing source Booster contract works; bootstrap is rejected when original baseline already contains a registry; existing job definitions cannot disappear or weaken under the same ID; admission forces full local release and rejects protected/trusted-CI contexts; admission-bearing manifests cannot become CI promotion evidence. Freeze/run bind both admission and review, including baseline and repository identity. No new scientific calculation, DB access, credential access, network tests, production code, or global installation.

Evidence: `test_modes_core.py:208–228` selects installed registry and then loads trusted Git registry at a fixed project path; dispatcher `:38–53` requires it for YAML changes. Core `:337–340` checked only ID inclusion, permitting command substitution under retained ID. Runner `:81–88` already pins candidate/base/registry; preserve that behavior. Existing `tests/test_test_modes_contract.py` provides disposable Git fixtures and public CLI coverage.

Implementation surface: core project lookup and exact execution/policy union (additive source-file coverage allowed); isolated admission module; dispatcher optional plan/run admission binding; focused unittest module reusing disposable Git fixtures. Existing test fixture copier gains the admission module. Architecture and dependency manifest describe the new edge. No bootstrap fallback for ordinary errors.

Root/independent challenge corrections: candidate jobs may append source coverage but cannot alter execution/policy or remove baseline sources. External evidence uses lstat before nonblocking no-follow open plus fstat identity verification, so a FIFO fails rather than hangs. Run independently derives the baseline-registry requirement from contract changes even if an admission field is stripped and the unkeyed manifest hash recomputed. These are explicit public CLI regressions.

Source-plan probe found pre-existing stale metadata: aggregate Python registry omitted `test_codex_loop.py`, and six impact mappings referenced removed individual Python job IDs. This successor adds both existing omitted/new tests to aggregate source coverage and maps stale IDs to the existing full Python aggregate; no execution command, required flag, or registered job is removed. Original root-cause inspection remains read-only in the notebook; these inventory repairs follow the direct source evidence and actual source-plan failure.

Falsifiers: candidate-only or symlink artifacts; missing pin/review; altered artifact/review/base/repo; same-ID command or policy change; removed job; admission over a existing registry; protected/trusted-CI invocation; omitted explicit pin at run. All must fail before jobs execute. Existing regression suite must remain passing. Source-code review is separate from runtime test evidence.

## Verification scope

`python3 -m unittest tests.test_project_contract_admission tests.test_test_modes_contract` exited 0: **34 tests passed in 11.170s**. Exact stdout/stderr are adjacent `project_contract_tests_20261010.*.log` files. This executes disposable infrastructure fixtures, not Horizon science.

Source `test_dispatcher.py plan --mode release --base ca0b3662b6a162a1054a2928d179638153c0a7de` exited 0 and selected all **44 required jobs**. This is a planning probe on a dirty implementation candidate, not a release run or promotion receipt. Full Booster suite and provider/network tests were not run. `git diff --check` passed. No global installation, source-repository edit, actual Horizon admission, or production deployment occurred.
