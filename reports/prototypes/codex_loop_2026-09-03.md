# Codex loop prototype notebook

- Timestamp: `2026-09-03T20:33:17Z`
- Environment: local macOS workspace `/Users/dmitrijnazarov/Projects/Claude_Booster`
- Operation class: `CLI_READ`; allowlist decision: `ALLOWED`
- Baseline: `codex-cli 0.153.0`

## Probe 1 — queue contract

Command: `codex queue --help`

Expected: an existing session can receive one message through explicit thread and message operands.

Actual: CLI exposes `codex queue [OPTIONS] --thread <THREAD> --message <TEXT>`; thread accepts a session UUID or exact session name. Exit `0`.

Invariant: `queue_thread == explicit_user_thread` is enforceable without session-store inference: **PASS**.

Raw-output reference: bounded output captured in the 2026-09-03 Codex session transcript; source executable reports `codex-cli 0.153.0`.

## Probe 2 — external reviewer version drift

Command: existing installed `zai_cli.py smoke --model glm-5.3 --budget 1` with prompt requiring exact `GLM_5_3_OK`.

Expected: determine whether the current Z.ai backend accepts exact model id `glm-5.3` without modifying routing.

Actual: stdout `GLM_5_3_OK`, exit `0`. Claude Code 2.1.259 emitted an `unrecognized_model` catalog warning, but the backend request succeeded.

Invariant: `requested_model == glm-5.3 && usable_response == true`: **PASS**.

## Source bindings

- `templates/scripts/zai_cli.py`: `a944c6a6e1db4b39d26312a734675fbc0823d8a3e12cbf1e3b9ec33463a2c365`
- `templates/scripts/model_balancer.py`: `bd468e7bea1f72accbd1e84c3fd2faa90ca5fa87d2b542e43520181f3b77bfd5`
- `install.py`: `ee0807aadbf48f7c1e33b3929952c914cff3a95e95b64066e9446aaea9bf6e85`

## Worker handoff

- Use an absolute resolved Codex executable and argv-based subprocess invocation.
- First wake occurs only after one full interval; sleep itself performs no Codex invocation.
- Use a fake Codex executable for lifecycle verification; never queue a live model message in tests.
- Preserve terminal state and make queue failure terminal.
- Treat GLM-5.3 as a separately verified routing migration; retain older pins only as legacy migration/history.
