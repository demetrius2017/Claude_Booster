# Consilium — Autonomous-Stretch Discipline (Opus 4.8 debt-bloat / context-loss fix)

**Date:** 2026-05-31
**Trigger:** Forensic finding that on `claude-opus-4-8` at default `high` effort, sessions inflate debt and lose context instead of finishing the task.
**Participants:** 3 Opus bio-agents (hook-runtime-engineer · agent-behavior-architect · risk-skeptic-devops) + GPT-5.5 (PAL `thinkdeep`, external).

---

## Task context — the proven diagnosis

Forensics over the 3 largest sessions of the last 2 days (`session_forensics.py`, all `claude-opus-4-8`):

| Signal | CRM-AI `5ae3ea8b` | horizon `4b0537c0` | horizon `feca4372` |
|---|---|---|---|
| Context peak (eff. input tok) | 566 K | **816 K** | 634 K |
| Max autonomous stretch (assistant-turns between two human prompts) | 143 | 144 | **220** |
| Compact events / "ran out of context" | 3 / 2 | 2 / 1 | 0 / 0 |
| One file re-read / re-edited | `srm_forecast_sync.py` **22× / 7×** | `snapshot_cron.py` 16× | `snapshot_cron.py` 12× / 11× |
| Bash vs Agent | 227 / 42 | 117 / 40 | **301 / 6** |
| debt-events | 0 | 21 | 21 |
| user interrupts | 3/36 | 1/21 | **7/15** |

**Causal chain:** Opus 4.8 + `high` effort → unregulated autonomous-stretch length (143–220 turns w/o human) → context bloat 5–7× the 120 K discipline line → ~800 K hard autocompact wall → lossy summary → thread loss → re-read/re-edit thrash + debt accretion. Amplifier: Lead does inline Bash (301:6) instead of delegating to context-isolated Workers. The control variable is **autonomous-stretch length**, not model IQ. (effort is not logged in JSONL; inferred from CC defaulting Max users to `high`.)

## Verified Facts Brief (code-truth)

- `PostToolUse` fires per tool call; `UserPromptSubmit` fires **only** on genuine human prompts (not tool-results, not `/goal`/`/loop` re-invokes). `Stop` fires once at turn end (or per `/goal` sub-turn).
- A long stretch is one human-to-human span, two sub-cases: **(1)** single long agentic turn — only PostToolUse fires mid-stretch, Stop once at end; **(2)** `/goal`-driven — Stop fires per re-invoke, UserPromptSubmit does not.
- `compact_advisor.py` (PostToolUse, token axis) writes a one-shot marker consumed by `compact_advisor_inject.py` at the **next human prompt** — structurally **cannot** reach the agent mid-stretch.
- Live `settings.json`: **no `continueOnBlock`**; compact_advisor always `return 0`. **Duplicate registrations** — compact_advisor ×3, ask_gate ×6 (PostToolUse/Stop), memory/model hooks ×N.
- `ask_gate.py` is a `Stop` hook that already blocks turn-end with `exit 2` (proven mid-flow agent-facing channel).
- `CLAUDE_EFFORT` is **readable** by hooks (`model_tag_enforcer.py:401`); currently `medium`. It is **not settable** (host-owned, `core.md` "Opaque host features").

---

## Decision

### Lever #1 — turns-since-human checkpoint: **APPROVE, phased & conditioned**

**Phase 1 (ship first — observe-only, ~zero risk).** New PostToolUse counter + a 3-line reset in the existing `compact_advisor_inject.py` (UserPromptSubmit). **Exit-0 only, logging only, no agent-facing injection.** Counter file `~/.claude/.turns_since_human_<sid>` (UUID-validated, atomic `os.replace`, `flock` around read-modify-write per FM-2). Resets on UserPromptSubmit (correct *by construction* — that event only fires on genuine human input), so `/goal` re-invokes accumulate. Logs `n` per call + sub-case markers to a JSONL. **Goal:** measure the real sub-case (1)/(2) split and stretch-length distribution before committing to a delivery channel + verify whether a post-autocompact "session continued" message fires UserPromptSubmit.

**Phase 2 (after data — delivery).** Deliver a **diagnostic, one-shot-per-stretch** advisory that fires only when **pathology markers co-occur** — turns ≥ T **AND** same-file re-reads heavy **AND** Bash:Agent ratio skewed — never on raw turn-count (FM-3 crying-wolf). Threshold effort-aware: T≈40 at `high`/`max`, ≈60 at `low`/`medium` (reading `CLAUDE_EFFORT` only to shape the hook's own advisory — the allowed side of the line). Channel, in priority of provenance:
- **Primary: `Stop`-hook checkpoint** — the only *proven* agent-facing mid-flow channel (ask_gate). Covers sub-case (2) every sub-turn and sub-case (1) at terminal Stop. **Must be ordered/coordinated with `ask_gate` to avoid a double-exit-2 wedge** (`core.md` warning).
- **Fallback (env-gated experiment only): single PostToolUse exit-2** at the arming call — *only* if Phase-1 data shows single-turn stretches dominate AND a throwaway-session canary proves PostToolUse exit-2 reaches the agent *before its next tool call*. Default off.

**Invariant (write into the module docstring):** the PostToolUse counter is **exit-0 forever**; the only exit-2 is the coordinated Stop checkpoint. No hook claims to detect/clear `/goal` or set effort.

### Lever #2 — effort differentiation: **APPROVE as PROSE ONLY (no hook)**

Unanimous: a hook whose *purpose* is "react to / advise on effort" is one refactor from the forbidden host-control pattern. Ship as rules:
- Generalize `goal-loop-discipline.md` §Effort-awareness: **effort polarity flips by phase** — asset in RECON/diagnosis/goal-halt, liability in IMPLEMENT autonomous stretches.
- New rule `autonomous-stretch-discipline.md` with the **checkpoint branch logic** (below). This prose helps *immediately*, even before the hook, via manual discipline + the existing token advisor.

**Checkpoint branch logic (the WHAT — agent executes first match, no permission-asking):**
1. Mid-atomic-op finishing in ≤3 turns → finish, then re-enter rule (one-shot grace, not a license).
2. Remaining work is delegable coding (≥20 LOC / unloaded file) → **STOP inline, spawn a context-isolated Worker** (the default branch — answers the 301:6 finding).
3. Genuine reversibility/blast-radius fork, <51% confident → return to human with the **single** decision (a real fork, not a permission-ask).
4. Else → `/compact`, continue.
Continuing the inline stretch past the ≤3-turn grace is the **one forbidden response**.

### Prerequisite — **deduplicate `settings.json` first**

Do not stack new machinery on triple/sextuple-registered hooks (FM-4: the cure feeds the disease). Collapse compact_advisor ×3→1, ask_gate ×6→canonical, etc., as a standalone prior change with its own verification.

---

## Rejected alternatives

| Rejected | Why |
|---|---|
| **Raw turn-count trigger** (turns ≥40 alone) | Crying-wolf (FM-3): interrupts healthy long *delegated* builds; agent learns to ignore it like it ignores 120 K. Must AND with pathology markers. |
| **PostToolUse `additionalContext` as mid-stretch channel** | Unsupported/unreliable in this CC line — the reason compact_advisor deliberately routes via the marker→UserPromptSubmit path. 3 independent confirmations. |
| **Lever #2 as a hook** | Reading a host-owned var *to steer behavior coupled to it* erodes the Opaque-host invariant for ~zero marginal value over prose. |
| **"Make UserPromptSubmit fire more / synthesize prompts / rely on continueOnBlock"** | Host-control claims `core.md` forbids; GPT flagged explicitly. |
| **Build full delivery now (skip observe phase)** | The sub-case (1)/(2) split is unknown; channel choice depends on it. Instrument-first is forced, not optional. |
| **Pure-prose, no hook at all** | The behavioral half *can* be prose, but the *detection* half cannot: an agent mid-stretch has no clock; a read-only PostToolUse counter is the one thing no rule can supply. That asymmetry earns Lever #1. |

## Risks (ranked, from the skeptic)

1. **Counter desync vs opaque host** (CRITICAL) — edge events (`[Request interrupted]`, post-autocompact synthetic "user", background-task completion) may falsely reset / never reset. → Phase-1 observe verifies which events fire UserPromptSubmit before any behavior depends on it.
2. **TOCTOU on the counter** (HIGH) — parallel tool calls + 3× registration race the increment (the bug the codebase already paid to remove). → `flock`; dedup registrations first.
3. **Crying wolf → signal death** (HIGH) — → AND with pathology markers; diagnostic not imperative; one-shot per stretch.
4. **Context pollution by the cure** (HIGH) — → one-shot, terse (<60 tok), dedup prerequisite.
5. **Exit-2 blast radius** (MED/catastrophic) — Stop double-block wedge / half-applied edits. → exit-0 invariant for the counter; Stop checkpoint coordinated with ask_gate; fallback env-gated + canary-verified.

## KPIs (pre → target)

| Metric | Baseline | Target |
|---|---|---|
| Max turns-since-human / session | 143–220 | ≤ 60 |
| Peak Lead context | 560–820 K | ≤ 200 K (delegation keeps bloat in Worker windows) |
| Autocompact-wall hits (PreCompact >500 K) | every long session | ~0 |
| Same-file re-read / re-edit | 22× / 7× | ≤ 3× / ≤ 1× |
| Bash:Agent in IMPLEMENT | 50:1 | ≤ 8:1 |
| debt-events / session | ~21 | ≤ 5 |
| user-interrupt rate | up to 47% | ≤ 7% |
| **Gating KPI** | — | **peak Lead context ≤ 200 K** (upstream cause; if it holds, the rest collapse) |

## Implementation recommendation (order)

1. **Dedup `settings.json`** (prerequisite) — own change + verify.
2. **Ship prose now** (zero risk): `autonomous-stretch-discipline.md` (branch logic) + `goal-loop-discipline.md` §Effort-awareness generalization.
3. **Phase-1 observe-only counter** — exit-0, logging only; run a few days.
4. **Read the data** → decide channel (Stop primary; PostToolUse-exit-2 fallback only if canary-verified) → **Phase-2 delivery** via `/go` (paired Worker+Verifier; high_blast_radius → Agent tool so PreToolUse guards fire).

**Open verification items** (must close before Phase-2): (a) does a post-autocompact "session continued" message fire UserPromptSubmit? (b) does PostToolUse exit-2 reach the agent mid-turn (canary)? (c) real sub-case (1)/(2) split.

---

## ADDENDUM Round-2 (2026-05-31) — effort is a FIRST-ORDER direct context polluter (v1 under-analyzed it)

**Critique that triggered this (user):** v1 demoted effort (Lever #2) to "prose only" by treating it as a *behavioral* amplifier (effort → longer stretch). That missed effort's **direct** context-pollution mechanism. Re-ran forensics (`effort_forensics.py`) measuring per-turn reasoning weight.

### New evidence

- **Thinking is stripped from the on-disk transcript** but billed in `usage.output_tokens`. All thinking blocks in the 3 sessions have empty text (keys `['signature','thinking','type']`) — the signature is kept for API replay, the reasoning text is not persisted.
- **Per-turn reasoning weight is the effort fingerprint:**

  | Session | turns >10k output | max output/turn | heavy turns w/ ≤1 tool |
  |---|---|---|---|
  | **feca4372** (worst-behaved) | **227 / 701 (32%)** | **64,000** (101 turns at the cap) | 227/227 |
  | 5ae3ea8b (CRM) | 50 / 1246 (4%) | 23,181 | 50/50 |

  Heavy turns have ≤1 tool call → the output is **reasoning, not tool args**. Session-wide for feca: ~22K tokens of text on disk + ~84K tokens of tool args, but ≈**6.95M** `output_tokens` generated → **~98% of generated tokens are thinking** that never appears in the transcript file.
- **`compact_advisor.py` measures the wrong quantity.** It stats transcript bytes//4 = 1.32M for feca, while the real live window (`usage` cache_read+input) peaked at 634K. It simultaneously over-counts (whole history ≠ live window) and is **blind to thinking** — the exact load that drives the real window. Thinking is replayed as input within an agentic turn → bloats the live window super-linearly during a long turn, independent of stretch length.

### Reframed model — TWO orthogonal, multiplicative channels

- **Channel A (behavioral, covered by Lever #1):** effort → longer autonomous stretch (143–220 turns).
- **Channel B (direct, MISSED in v1):** effort → heavier reasoning per turn (up to the 64K thinking-budget cap, 227× in feca) → live window inflates from the *first* heavy turn, no 40-turn wait needed.

feca maxed both (220-turn stretch × 64K-reasoning turns) → 634K window + worst thrash/interrupts. Lever #1 addresses only A. Channel B had no lever.

### Revised decision — Lever #2 promoted from "prose only" to code + prose

- **2a (NEW, highest ROI): fix `compact_advisor` to estimate the window from `usage` (cache_read+input of the last assistant turn in the transcript), not file bytes.** This is the only change that makes the existing bloat detector *see* effort-driven thinking. Pure measurement fix; does not touch the opaque-host boundary. **Do this regardless of Lever #1.**
- **2b (NEW): per-turn-weight axis.** Sustained high `output_tokens/turn` (repeated >10–20k) during IMPLEMENT = effort-pollution fingerprint → advisory. Measuring the fingerprint, not setting effort (allowed, mirrors `model_tag_enforcer.py:401`).
- **2c (kept, now numerically justified): phase-aware effort polarity** prose — at IMPLEMENT with sustained 64K-reasoning turns, surface to the human once: "consider `/effort medium` for this build phase." Asset in RECON/diagnosis, liability in IMPLEMENT.

### Revised implementation order

0. **Lever #2a first** (usage-based token estimate in compact_advisor) — cheapest, unblocks accurate measurement for everything downstream, and is the single fix that makes effort-bloat visible.
1–4. unchanged (dedup settings.json → prose rules → observe-only counter → phased delivery).

**Note:** v1's "Open verification item (c) sub-case split" is now joined by **(d): does usage-based estimation in compact_advisor correctly track the live window across an autocompact boundary?**
