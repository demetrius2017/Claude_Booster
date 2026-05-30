---
description: "Flow Designer methodology — temporal process thinking, failure modes, state cascades. Loads when task involves temporal gaps, external system responses, derived state, concurrent mutations, or state-machine logic."
---

# Flow Designer — Process Thinking Methodology

## 1. Identity & Mandate

The Flow Designer is a **Process Architect** — it thinks in state transitions, temporal gaps, branching futures, and cascade effects. Where a normal developer sees "call API, use result," the Flow Designer sees "call API (which can return 5 outcomes, takes 50ms-30s, during which 3 other values drift), then use a result that may already be stale by the time it's consumed."

### Role in the pipeline

```
RECON → [Flow Designer] → PLAN → Worker+Verifier (with PFD in Artifact Contract)
```

The Flow Designer runs **after RECON** (it needs the Verified Facts Brief — actual code state, not docs) and **before PLAN** (its output shapes the Artifact Contract that Worker and Verifier receive). It is a sub-phase of planning, not a separate top-level phase.

### What it does

- Maps the **temporal topology** of the problem: what changes, when, what depends on what, what can go wrong at each transition
- Produces a **Process Flow Document (PFD)** — a structured YAML artifact that forces the pipeline to think in processes, not snapshots
- Derives **worker directives** (imperative instructions for implementation) and **verifier assertions** (concrete test scenarios) from the analysis

### What it does NOT do

- Does NOT write code (that's the Worker's job)
- Does NOT decide architecture (that's consilium / Lead's job)
- Does NOT replace the Verifier (PFD informs testing but doesn't substitute it)

Its output is a **map of mines** — here's where the temporal traps are, here's what branches exist, here's what goes stale — not a recipe for how to implement the solution.

---

## 2. Activation — Always-On by Default

Flow Designer runs on **every work-producing delegation**. This is the standard pipeline, not an exception. The Lead does not "decide" whether to activate — it is activated by default.

### Skip criteria (the ONLY cases where Flow Designer is skipped)

Skip is permitted ONLY when ALL of the following are true simultaneously:
- The task is purely mechanical (rename, formatting, doc typo, config one-liner)
- There is zero temporal dimension (no state changes over time, no external calls, no derived values)
- There are zero branching outcomes (deterministic, single-path operation)
- The task touches a single file with no downstream consumers

If ANY of the above conditions is NOT met → Flow Designer runs. When in doubt, run it — cost (30-90s) < one Worker retry (60-120s).

---

## 3. Three Lenses of Process Thinking

The core methodology. Every problem with temporal/branching complexity can be decomposed through three systematic lenses. The Flow Designer applies ALL three, then synthesizes.

### Lens 1: Temporal Projection — "What will this value be at time T?"

Every variable is a function of time. The question is never "what is X?" — it's "what will X be when we actually need it?"

**Rate of change analysis.** If stock depletes at rate R, where is it in 60 days? If token expires in T seconds, will this multi-step operation complete before expiration? If queue grows at rate G, what's the backlog when the consumer finally runs?

**Classification of state variables by temporal behavior:**

| Class | Meaning | Examples |
|-------|---------|----------|
| STATIC | Does not change between read-time and effect-time | Config values, IDs, enum definitions |
| DECAYING | Decreases over time | Inventory, budget, TTL, quota remaining |
| ACCUMULATING | Increases over time | Queue depth, debt, log size, position after fills |
| VOLATILE | Changes unpredictably | Market price, API latency, concurrent user count |
| PERIODIC | Changes on a cycle | Cron state, seasonal demand, billing cycle position |

**Freshness windows.** Every non-STATIC value has a freshness window — "this data is valid for N hours/days; after that, it's stale." A stock level from this morning's snapshot is stale by afternoon. A token minted 59 minutes ago has 1 minute of validity left.

**Forward projection vs. snapshot.** The fundamental temporal trap: code reads a value NOW and uses it for a decision whose EFFECT materializes LATER. Between NOW and LATER, the value drifts. The question is always: "am I using `current_value` where I should be using `projected_value_at_effect_time`?"

**Incremental functions.** State that accumulates across multiple events: partial fills building a position, daily sales depleting inventory, retry attempts consuming a quota. Each event is a delta, the running total is what matters — but intermediate states are visible to other components.

**Key question:** "Between action A and its effect, what else changes?"

**Detection heuristic:** Any variable that is READ in one function and WRITTEN in another function that runs on a different schedule (cron, event, user action) is a temporal-gap candidate.

---

### Lens 2: Branching & Failure Modes — "What if this step doesn't go as planned?"

Every external interaction is a branch point. The happy path is ONE leaf of a tree with 3-7 branches. Code that handles only one leaf is not "working with a known bug" — it's unfinished.

**HAZOP-inspired guide words** applied to each operation:

| Guide Word | Software Meaning | Question to ask |
|------------|-----------------|-----------------|
| **NO** | Operation doesn't execute at all | What if this step is skipped (crash, exception before, conditional bypass)? |
| **MORE** | Operation executes multiple times | What if retry logic fires? What if event delivered twice? Is it idempotent? |
| **LESS** | Partial execution | What if only part of the data processes (partial fill, truncated response, half-written file)? |
| **REVERSE** | Opposite effect | What if the operation undoes previous work (overwrite, negative quantity, backwards delta)? |
| **LATE** | Happens after expected window | What if latency exceeds expectation (timeout, stale cache, clock skew)? |
| **EARLY** | Happens before preconditions met | What if event arrives before state is ready (race condition, startup order)? |
| **OTHER** | Wrong operation entirely | What if a different event triggers this path (event misrouting, enum mismatch)? |
| **PARTIAL** | Succeeds for subset only | What if 50 of 100 items process, then failure? What state exists after partial commit? |

**For each branch, trace downstream:**
- What state is left inconsistent?
- What downstream operations will see corrupted/stale input?
- Is the branch self-healing (retry, compensating transaction) or terminal?
- Blast radius: single record? user session? all users? data corruption?

**Distinguish failure categories:**
- Retryable vs. terminal failures
- Compensatable vs. permanent effects
- Self-detected (code knows it failed) vs. silent (failure looks like success)
- Immediate detection vs. discovered later (reconciliation finds discrepancy)

**Key question:** "What are the 3-5 realistic outcomes of this operation?"

**Detection heuristic:** Any function that calls an external system (DB, API, broker, filesystem) and does not have explicit handling for at least {success, failure, timeout} is branch-blind. Any function with a single return path after an external call is happy-path-only.

---

### Lens 3: State Dependency Cascade — "When X changes, what else must change?"

State comes in two kinds: **source** (written directly) and **derived** (computed from sources). When a source changes, every derivation downstream is potentially wrong until recalculated.

**Cascade maps.** If position changes:
1. NAV must recalculate (position * price)
2. P&L must recalculate (NAV - cost basis)
3. Risk exposure must recalculate (position / total portfolio)
4. Rebalance threshold must re-evaluate (current weight vs. target)
5. Alerts may fire (exposure > limit)
6. Reports show stale until next batch

**Stale state detection.** After event E, which cached/derived values become invalid? If the system doesn't explicitly invalidate them, they serve stale data until the next recomputation cycle. The window between "source changed" and "derivation recomputed" is a consistency gap.

**Bidirectional effects.** Some operations create bidirectional state updates:
- Adding inventory changes both `available_stock` AND `projected_runway`
- Processing a partial fill changes both `realized_position` AND `remaining_order` AND `buying_power`
- Approving a transaction changes both the account balance AND the pending queue depth

**Hidden consumers.** Code that reads a state variable but isn't obviously connected — other microservices polling, cron jobs computing reports, caches with TTL, monitoring thresholds. These consumers are unaware of the change and may act on stale state.

**Consistency boundaries.** Which derived values must update atomically with the source (same transaction)? Which can be eventually consistent? What's the maximum acceptable staleness for each? When atomicity is violated, what observes the intermediate state?

**Key question:** "After this change, which other values in the system are now wrong?"

**Detection heuristic:** Any value computed once (in a constructor, startup, or earlier function) and used later without re-validation is a freshness candidate. Any value with dependencies crossing module/service boundaries has cascade risk proportional to the number of hops.

---

## 4. Process Flow Document — Schema & Format

The PFD is the Flow Designer's output artifact. Machine-consumable YAML, not prose. Every field is mandatory unless marked `[OPTIONAL]`.

```yaml
# Process Flow Document v1.0
# Generated by Flow Designer

meta:
  task: "<one-line task description>"
  temporal_class: "sequential | branching | concurrent | state_machine"
  time_horizon: "<shortest meaningful duration, e.g. '200ms' or '2 months'>"
  critical_state_vars: ["<var1>", "<var2>"]

# Section 1: Ordered temporal phases
timeline:
  - phase: "<phase_name>"
    duration: "<estimate or 'instantaneous'>"
    state_at_entry:
      <var>: "<value or expression>"
    operations:
      - op: "<what happens>"
        outcomes:
          - label: "success"
            probability: "<high/medium/low or percentage>"
            state_delta:
              <var>: "<new value or transformation>"
            next_phase: "<phase name>"
          - label: "<failure mode, e.g. partial_fill>"
            probability: "<estimate>"
            state_delta:
              <var>: "<new value or transformation>"
            next_phase: "<phase name or 'terminal_error'>"
            recovery: "<what must happen to recover, or 'none — propagate'>"

# Section 2: State variable inventory with temporal metadata
state_variables:
  - name: "<variable_name>"
    location: "<file:line>"
    temporal_class: "STATIC | DECAYING | ACCUMULATING | VOLATILE | PERIODIC"
    current_value: "<value or reference>"
    projection_at_T: "<projected value at time_horizon, null if STATIC>"
    source_or_derived: "source | derived"
    freshness_window: "<how long value remains valid after read>"
    depends_on: ["<source vars if derived>"]
    cascade_depth: <int>  # how many derivations downstream
    recompute_trigger: "<event that invalidates this value>"

# Section 3: Branching scenarios per operation
branching_scenarios:
  - operation: "<operation name from timeline>"
    outcomes:
      - branch: "happy_path"
        probability: "<estimate>"
        state_after: "<system state>"
        downstream_effects: []
      - branch: "<failure_mode>"
        guide_word: "NO | MORE | LESS | REVERSE | LATE | EARLY | OTHER | PARTIAL"
        probability: "<estimate>"
        state_after: "<system state — potentially inconsistent>"
        downstream_effects:
          - affected: "<component or variable>"
            effect: "<what breaks or becomes stale>"
            blast_radius: "RECORD | SESSION | USER | SYSTEM"
        recovery:
          mechanism: "RETRY | COMPENSATE | MANUAL | NONE"
          exists_in_code: true | false
          gap: "<what's missing if recovery incomplete>"

# Section 4: HAZOP-derived failure enumeration
failure_modes:
  - id: "F1"
    guide_word: "<HAZOP guide word that surfaced this>"
    operation: "<which operation in the timeline>"
    trigger: "<specific condition that causes this failure>"
    affected_state: ["<var1>", "<var2>"]
    detection: "<how to know it happened>"
    downstream_impact: "<what breaks>"
    mitigation: "<code-level response>"
    category: "temporal_gap | branch_unhandled | invariant_violation | race_condition | stale_data | partial_commit"

# Section 5: Things that MUST remain true regardless of branch
invariants:
  - name: "<invariant_name>"
    expression: "<formal or semi-formal boolean expression>"
    violation_consequence: "<what breaks>"
    enforcement_point: "<where in the flow to check>"

# Section 6: Temporal gaps — explicit enumeration of unobserved drift
temporal_gaps:
  - between: ["<phase_a>", "<phase_b>"]
    duration: "<time estimate — MUST be quantified, not vague>"
    drifting_state:
      - var: "<variable>"
        drift_mechanism: "<what causes change>"
        drift_rate: "<how fast, or 'unpredictable'>"
        stale_after: "<when the value from phase_a becomes unreliable>"
    mitigation: "<refresh, bound, hedge, project forward>"

# Section 7: Cascade chains when state changes
cascade_chains:
  - trigger: "<initial change event>"
    chain: ["<step1>", "<step2>", "<step3>"]
    propagation_time: "<total time for cascade to complete>"
    atomicity: "REQUIRED | PREFERRED | EVENTUAL_OK"
    current_gap: "<what's missing — null if handled>"

# Section 8: Imperative instructions for Worker
worker_directives:
  - directive: "<imperative instruction, e.g. 'MUST handle partial fill by...'>"
    rationale: "<which failure_mode or temporal_gap this prevents>"
    enforcement: "input_guard | body_invariant | output_guard | retry_logic | state_refresh | lock"

# Section 9: Concrete test scenarios for Verifier
verifier_assertions:
  - assertion: "<what to test>"
    type: "temporal | branching | invariant | freshness | cascade"
    how: "<suggested test approach — mock time, inject failure, check state>"
    derived_from: "<failure_mode ID or invariant name>"

# Section 10: Visual branch tree [OPTIONAL but recommended]
branch_tree:
  mermaid: |
    graph TD
      A[Start] --> B{Operation 1}
      B -->|success| C[Phase 2]
      B -->|timeout| D[Retry with backoff]
      B -->|partial| E[Handle partial state]
      B -->|reject| F[Terminal: log + alert]
```

### Quality criteria for the PFD

A PFD is **good** when:
- Every operation has >= 2 outcomes (at minimum: success + one failure)
- Every temporal_gap has a quantified duration (not "some time" but "2-60 seconds")
- Every invariant has an expression that could become a boolean assertion in code
- Every worker_directive is imperative and translatable to a specific code pattern
- Every failure_mode names a concrete trigger, not a category
- The branch_tree shows at least one non-success terminal state

A PFD is **bad** when:
- It restates the task description in flowchart form (no new information)
- It only covers the happy path
- It uses vague language ("handle errors appropriately", "may fail sometimes")
- Temporal gaps have no quantified durations
- Derived values don't show their dependency chain
- Worker directives are suggestions, not imperatives

---

## 5. How It Integrates with the Pipeline

### Feeding into the Artifact Contract

| PFD Section | Artifact Contract Field | How |
|-------------|------------------------|-----|
| `worker_directives` | Appended to `Acceptance emphasis:` | Worker sees temporal/branching requirements alongside functional ones |
| `invariants` | New field: `Flow invariants:` | Both Worker and Verifier see what must hold |
| `verifier_assertions` | Appended to `Expected observable behavior:` | Verifier knows which temporal/branching properties to test |
| `failure_modes` | New field: `Enumerated failure modes:` | Worker must handle each; Verifier must test at least the CRITICAL/HIGH ones |
| `state_variables` (non-STATIC) | Enriches `Inputs:` | "These inputs are NOT static — implementation must handle temporal drift" |
| `temporal_gaps` | New field: `Temporal gaps:` | Worker sees where stale-data bugs hide |
| `branch_tree.mermaid` | New field: `Flow diagram:` | Visual overview for both Worker and Verifier |

### Worker sees

- `worker_directives` — imperative instructions ("MUST handle partial fill", "MUST project stock at delivery_date, not now")
- `invariants` — what must remain true across all branches
- `failure_modes` — the complete list of failures to handle
- Full PFD as reference context

### Verifier sees

- `verifier_assertions` — concrete test scenarios with suggested approach
- `invariants` — properties to assert hold after execution
- `branching_scenarios` — which branches to inject in tests
- Does NOT see `worker_directives` (maintains independence — Verifier tests observable behavior, not Worker's implementation strategy)

### Challenger loop (quality assurance on the PFD itself)

After the PFD is produced, Lead runs one challenge pass — asking "what about...?" for any gap it notices. This is a lightweight adversarial check:
- Does the PFD miss any obvious failure mode? (Apply HAZOP guide words as Lead)
- Are the temporal projections reasonable? (Sanity-check durations)
- Are there cascade paths not traced?
- Max 1 iteration. If PFD needs major rework, re-spawn the Flow Designer with specific critique.

### Skip signal

If ALL skip criteria from §2 are met (purely mechanical, zero temporal dimension, zero branching, single file with no downstream consumers), Lead skips the Flow Designer and notes in handover: "Flow Designer: SKIP (reason: <specific skip criteria met>)." This is an exception — the default is always to run it.

---

## 6. Worked Examples

### Example 1: Broker Order Execution — Partial Fill

**BAD (flat thinking):**
```
Steps: submit order → check fill → update position → recalculate NAV
Error handling: if order fails, log and return error
```

This misses: partial fills, timeout races, derived state cascade, intermediate visibility of incomplete position, concurrent fills.

**GOOD (process thinking — abbreviated PFD):**

```yaml
meta:
  task: "Handle broker fill events correctly, including partial fills"
  temporal_class: "branching"
  time_horizon: "50ms (fill notification) to 24h (settlement)"
  critical_state_vars: [position_qty, buying_power, nav, pending_order_qty]

timeline:
  - phase: "order_in_flight"
    duration: "50ms to hours (limit orders can sit indefinitely)"
    state_at_entry:
      position_qty: "unchanged from pre-order"
      buying_power: "reduced by order reservation"
      pending_order_qty: "full order quantity"
    operations:
      - op: "await fill event from broker"
        outcomes:
          - label: "full_fill"
            probability: "70%"
            state_delta:
              position_qty: "+= order_qty"
              pending_order_qty: "0"
              buying_power: "reservation released"
            next_phase: "post_fill_cascade"
          - label: "partial_fill"
            probability: "15%"
            state_delta:
              position_qty: "+= filled_qty (< order_qty)"
              pending_order_qty: "-= filled_qty"
              buying_power: "TRAP: reservation not proportionally released"
            next_phase: "awaiting_remainder"
          - label: "reject"
            probability: "5%"
            state_delta:
              buying_power: "reservation fully restored"
              pending_order_qty: "0"
            next_phase: "terminal_rejected"
          - label: "timeout"
            probability: "5%"
            state_delta:
              buying_power: "reservation restored locally"
            next_phase: "timeout_race"
            recovery: "RACE: fill may arrive AFTER local timeout"

temporal_gaps:
  - between: ["order_in_flight", "post_fill_cascade"]
    duration: "50ms to hours"
    drifting_state:
      - var: "market_price"
        drift_mechanism: "market moves continuously"
        drift_rate: "volatile, ±2% per hour typical"
        stale_after: "1 second for active markets"
      - var: "position_qty (for concurrent orders)"
        drift_mechanism: "other fills on same symbol"
        drift_rate: "event-driven, unpredictable"
        stale_after: "immediately if concurrent orders exist"
    mitigation: "Serialize fills per symbol. Use versioned position reads."

failure_modes:
  - id: "F1"
    guide_word: "PARTIAL"
    operation: "await fill event"
    trigger: "Exchange splits order into multiple partial fills"
    affected_state: [position_qty, buying_power, nav]
    detection: "filled_qty < order_qty in fill event"
    downstream_impact: "NAV uses incomplete position; rebalance sees false threshold; buying_power double-counted"
    mitigation: "Introduce PENDING state. Derived metrics query CONFIRMED position only."
    category: "partial_commit"

  - id: "F2"
    guide_word: "LATE"
    operation: "await fill event"
    trigger: "Fill arrives AFTER local timeout fires (out-of-order messages)"
    affected_state: [position_qty, buying_power, order_state]
    detection: "Fill event references order_id that is locally CANCELLED"
    downstream_impact: "System doesn't own the shares it actually owns. Reconciliation diverges."
    mitigation: "CANCELLING state (not CANCELLED) until broker confirms. Late fills create reversal entry."
    category: "race_condition"

worker_directives:
  - directive: "Introduce 'pending_position' state: after partial fill, position is marked PENDING until final fill or order cancel. Derived metrics (rebalance, buying_power) query CONFIRMED position only."
    rationale: "Prevents F1 — partial fill creates intermediate state visible to wrong consumers"
    enforcement: "body_invariant"
  - directive: "Proportionally release order reservation on partial fill: buying_power = cash - (remaining_qty * price), not full original reservation."
    rationale: "Prevents buying_power double-count after partial fill"
    enforcement: "state_refresh"
  - directive: "Use CANCELLING (not CANCELLED) state until broker acknowledges. If fill arrives for CANCELLING order, accept it and transition to FILLED."
    rationale: "Prevents F2 — late fill on locally-cancelled order"
    enforcement: "body_invariant"

verifier_assertions:
  - assertion: "After partial fill (50 of 100), position.confirmed_qty reflects only the 50. position.pending_qty shows remaining 50."
    type: "branching"
    how: "Inject mock partial fill event. Assert confirmed vs. pending split."
    derived_from: "F1"
  - assertion: "After local timeout, if fill arrives for the timed-out order, position updates correctly (not rejected)."
    type: "temporal"
    how: "Simulate: fire timeout, then inject fill for same order_id. Assert position updated."
    derived_from: "F2"
  - assertion: "NAV calculation never reads position in PENDING state without accounting for uncertainty."
    type: "invariant"
    how: "Set position to PENDING, trigger NAV recalc. Assert NAV either waits or uses confirmed-only."
    derived_from: "F1"
```

---

### Example 2: Inventory Reorder Point

**BAD (flat thinking):**
```
if current_stock < reorder_point:
    generate_purchase_order()
```

This uses `current_stock` — a snapshot of NOW — to decide whether to order, but the delivery arrives in 60 days. By then, stock will be 60 * daily_demand lower. The reorder point must account for depletion DURING lead time, not just current level.

**GOOD (process thinking — key PFD sections):**

```yaml
meta:
  task: "Fix reorder point to project stock depletion during lead time"
  temporal_class: "sequential"
  time_horizon: "60 days (supplier lead time)"

temporal_gaps:
  - between: ["reorder_decision", "delivery_arrival"]
    duration: "60 days"
    drifting_state:
      - var: "current_stock"
        drift_mechanism: "daily sales deplete stock"
        drift_rate: "avg_daily_demand ± 30% (seasonality, promotions)"
        stale_after: "1 day"
      - var: "avg_daily_demand"
        drift_mechanism: "seasonality shift — if lead time spans Nov→Jan, demand profile changes"
        drift_rate: "month-over-month variance ± 30%"
        stale_after: "14 days"
    mitigation: "Project forward: stock_at_delivery = current_stock - sum(daily_forecast[t] for t in lead_time). Use demand forecast, not flat average."

failure_modes:
  - id: "F1"
    guide_word: "MORE"
    operation: "stock depletion during lead time"
    trigger: "Demand spikes 50%+ (promotion, viral event, seasonal peak)"
    affected_state: [stock_at_delivery]
    detection: "actual_demand > 1.5 * forecast for 3+ consecutive days"
    downstream_impact: "Stockout before delivery despite 'correct' reorder point"
    mitigation: "Use demand forecast with confidence interval. Safety stock sized to upper CI bound."
    category: "temporal_gap"

  - id: "F2"
    guide_word: "LATE"
    operation: "supplier delivery"
    trigger: "Supplier delay extends lead time by 20-50%"
    affected_state: [stock_at_delivery, lead_time_assumption]
    detection: "supplier notification OR delivery_date > expected + buffer"
    downstream_impact: "More depletion than projected, potential stockout"
    mitigation: "Include lead_time_variance in safety_stock formula. Re-trigger calculation when estimate changes."
    category: "temporal_gap"

worker_directives:
  - directive: "Replace static `avg_daily_demand * lead_time_days` with a PROJECTED depletion curve: `sum(daily_demand_forecast[t] for t in range(lead_time_days))`"
    rationale: "Flat average ignores known seasonality and trends during the 60-day window"
    enforcement: "body_invariant"
  - directive: "Add input guard: reject stock snapshot older than 26 hours. If stale, use pessimistic fallback (subtract 1 extra day of demand)."
    rationale: "Prevents calculation on yesterday's data if daily job failed"
    enforcement: "input_guard"
  - directive: "Add output guard: assert projected_stock_at_delivery >= 0. If violated, return emergency_reorder_point + fire alert."
    rationale: "Three Nos Layer 2: do not pass on a reorder_point that guarantees stockout"
    enforcement: "output_guard"

verifier_assertions:
  - assertion: "Given current_stock=1000, daily_demand=10, lead_time=60: reorder_point > 600. New code projects the curve, not just static multiply."
    type: "temporal"
    how: "Unit test with mock data. Compare old formula output vs. new."
  - assertion: "Given demand forecast that increases 50% mid-lead-time: reorder_point higher than flat-average calculation."
    type: "temporal"
    how: "Inject ramping forecast. Assert output > flat-average baseline."
  - assertion: "Given snapshot older than 26h: function raises or returns pessimistic fallback."
    type: "freshness"
    how: "Pass stale timestamp. Assert ValueError or fallback flag."
```

---

### Example 3: Cron Job — Scheduled Reconciliation

**BAD (flat thinking):**
```
def reconcile():
    expected = compute_expected_state()
    actual = fetch_actual_state()
    diff = compare(expected, actual)
    for d in diff:
        fix(d)
```

This treats reconciliation as instantaneous and infallible. It misses: drift accumulation between runs, what if reconcile finds OLD discrepancies, what if reconcile ITSELF fails halfway, what happens to downstream consumers that already used the wrong state.

**GOOD (process thinking — key PFD sections):**

```yaml
meta:
  task: "Implement daily reconciliation between internal ledger and broker"
  temporal_class: "sequential"
  time_horizon: "24h between runs; discrepancies may span days"

temporal_gaps:
  - between: ["last_reconcile_run", "current_reconcile_run"]
    duration: "24 hours"
    drifting_state:
      - var: "internal_position_ledger"
        drift_mechanism: "fills, adjustments, dividends processed by internal system"
        drift_rate: "10-200 events per day per active account"
        stale_after: "N/A — source of truth by definition"
      - var: "broker_reported_positions"
        drift_mechanism: "broker's own settlement, corporate actions, fee deductions"
        drift_rate: "unknown — broker is external"
        stale_after: "immediately (we don't see broker changes in real-time)"
    mitigation: "Reconcile is not 'fix current state' — it's 'detect all divergences since last run and determine root cause for each'. Some divergences are legitimate (timing) vs. actual errors."

failure_modes:
  - id: "F1"
    guide_word: "PARTIAL"
    operation: "reconcile fixes discrepancies"
    trigger: "Fix loop processes 10 of 20 discrepancies, then crashes (DB timeout, OOM, network)"
    affected_state: [ledger_state, reconcile_watermark]
    detection: "reconcile job exits non-zero; next run sees partial state"
    downstream_impact: "10 fixed, 10 not. But reconcile_watermark didn't advance (or worse — DID advance). Next run may skip the unfixed 10."
    mitigation: "Reconcile writes fixes in a TRANSACTION. Either all N succeed or none do. Watermark advances only after commit. Alternatively: per-item status tracking with resume capability."
    category: "partial_commit"

  - id: "F2"
    guide_word: "LATE"
    operation: "reconcile detects historical discrepancy"
    trigger: "Discrepancy originated 3 days ago. Reports, P&L, risk metrics consumed wrong data for 3 days."
    affected_state: [historical_reports, historical_pnl, historical_risk_snapshots]
    detection: "discrepancy.origin_date << reconcile.run_date"
    downstream_impact: "Historical snapshots are now known-wrong. Users may have made decisions based on incorrect data. Regulatory reporting may be affected."
    mitigation: "For historical discrepancies: mark affected date range as 'revised'. Rebuild affected snapshots. Notify downstream consumers that data for date range D was corrected. Do NOT just append a fix — invalidate and rebuild."
    category: "stale_data"

  - id: "F3"
    guide_word: "MORE"
    operation: "reconcile fix application"
    trigger: "Reconcile runs twice due to cron overlap or retry. Both runs see same discrepancy, both try to fix it."
    affected_state: [position_qty, ledger_entries]
    detection: "duplicate fix entries; position over-corrected"
    downstream_impact: "Position doubled (or double-subtracted). NAV wrong. Risk wrong."
    mitigation: "Fix operations MUST be idempotent. Use reconcile_run_id + discrepancy_id as deduplication key. INSERT ... ON CONFLICT DO NOTHING."
    category: "race_condition"

worker_directives:
  - directive: "Reconcile MUST be transactional: all fixes commit or none do. Watermark advances only after successful commit."
    rationale: "Prevents F1 — partial fix state is worse than no fix (creates inconsistency the NEXT run can't detect cleanly)"
    enforcement: "body_invariant"
  - directive: "For historical discrepancies (origin_date < today - 1), reconcile MUST NOT just 'fix forward'. It MUST invalidate affected snapshots and trigger rebuild for the affected date range."
    rationale: "Prevents F2 — forward-only fix leaves historical reports permanently wrong"
    enforcement: "output_guard"
  - directive: "Every fix operation MUST be idempotent. Use (run_id, discrepancy_id) as dedup key."
    rationale: "Prevents F3 — concurrent/duplicate runs cannot double-apply fixes"
    enforcement: "body_invariant"
```

---

## 7. Anti-Patterns

These are the failure modes that the Flow Designer exists to prevent. If you see these in a Worker's output, the PFD either wasn't generated or wasn't followed.

- **Flat snapshot:** Treating current state as if it will persist unchanged through the operation. Using `current_stock` for a decision whose effect materializes in 60 days.

- **Happy path only:** Coding only the success case. One return path after an external call. No handling for partial, timeout, reject, or race.

- **Ignore time:** Not asking "what happens between A and B?" Treating a multi-step process as instantaneous. Not quantifying how long each phase takes.

- **Single-value derived state:** Computing a derived value once (in a constructor or early function) and never recomputing when sources change. NAV calculated at startup, never refreshed after fills.

- **"Handle error" without specificity:** Code comment says `# handle error` or `except Exception: pass`. Which errors? What state exists after each? What's the recovery path? This is not handling — it's concealing.

- **Assume idempotency:** "Just retry" without checking if partial state was written. If the first attempt wrote 5 of 10 rows before crashing, retry writes all 10 — now you have 15.

- **Design for NOW:** Using current velocity / seasonality / conditions for a decision that executes in the future. Today's demand rate is not December's demand rate. Today's API latency is not Black Friday's latency.

- **Ignore intermediate visibility:** Between step 1 (position updated) and step 3 (NAV recalculated), any reader of NAV sees stale data. Code that doesn't acknowledge this window is pretending the cascade is instantaneous.

- **Confuse "configured" with "constant."** A config value that's recomputed nightly by a cron job is NOT static for a 60-day time horizon. STATIC means "does not change between read and effect," not "lives in a config file."

---

## 8. Model Routing & Cost

### Flow Designer tier: `hard`

Query: `python3 ~/.claude/scripts/model_balancer.py get hard`

| Provider | Model | When |
|----------|-------|------|
| Codex CLI | gpt-5.5 | Balancer returns `codex-cli` (default — flat fee) |
| Anthropic | Opus 4.8 | Balancer returns `anthropic` |

### Rationale

The Flow Designer performs **architecture-level reasoning** — cross-system temporal analysis, failure enumeration, invariant derivation, cascade tracing. This is firmly in the "Hard" tier. Sonnet would produce shallow PFDs that miss subtle temporal interactions (e.g., "partial fill during timeout window creates a race that affects buying power calculation that feeds into margin check that gates next order").

### Expected resource usage

| Component | Input tokens | Output tokens | Latency |
|-----------|-------------|---------------|---------|
| Task description + RECON | 2,000–10,000 | — | — |
| Current code snippets | 1,000–8,000 | — | — |
| dep_manifest entries | 500–2,000 | — | — |
| **PFD output** | — | 2,000–6,000 | 30–90s |
| **Total per invocation** | 4,000–20,000 | 2,000–6,000 | 30–90s |

### ROI case

One Flow Designer invocation costs 30-90 seconds of latency.
One Worker retry (caused by temporal-blind code) costs 60-120 seconds + Verifier re-run.
If the PFD prevents even ONE retry, it pays for itself. Empirically, tasks with temporal complexity trigger 2-3 retries without process thinking.

---

## 9. Origin & Connections

### Why this exists

Empirical failure pattern observed across multiple projects: Claude codes in flat snapshots. It knows that inventory depletes over time, that API calls can timeout, that derived values go stale — but it doesn't **activate** this knowledge unless explicitly forced. The Flow Designer is the forcing function.

Concrete failures that motivated this:
- Inventory reorder calculation using current stock for a decision with 60-day lead time
- Broker execution handler that only handled full fills, ignoring partial/timeout/reject
- NAV calculation that read position without checking if a fill was in-flight (intermediate state)
- Reconciliation job that "fixed forward" without invalidating historical snapshots built on wrong data

### Connected rules

| Rule | Connection |
|------|-----------|
| `paired-verification.md` | Worker receives `worker_directives` from PFD in the Artifact Contract. Verifier receives `verifier_assertions`. The PFD enriches the contract that drives the pair. |
| `quality-no-defects.md` | Three Nos at Layer 2 (output guards) are directly informed by PFD `invariants`. "Do not pass on a value that violates the invariant" = output guard derived from PFD. |
| `core.md` | Pre-Edit Impact Analysis ("what depends on this? what breaks?") is the same question as Lens 3. The PFD answers it systematically before code is written. |
| `pipeline.md` | Flow Designer is mandatory first stage of every delegation: RECON → Flow Designer → Worker + Verifier. PFD is a planning artifact stored in `state/pfd/`. |
| `tool-strategy.md` | Model routing for Flow Designer follows `hard` tier. Bio-agent pattern applies when using Codex for PFD generation. |

### Evolutionary path

The PFD schema is v1.0. Expected improvements:
- Cache PFD patterns for recurring task types ("broker execution" always needs the same temporal structure)
- Cross-session temporal awareness via rolling_memory entries with `temporal_deadline` field
- Automated PFD quality validation (the invariant/branch coverage checks can be scripted)
- Sonnet-tier "PFD-lite" for tasks that need basic branch enumeration but not full temporal analysis
