# Same-class automatic model updates — installed verification

Implemented and installed on 2026-09-27. SessionStart starts a nonblocking daily
refresh; exact candidates pass fixture/tool, arithmetic and code-expression
checks before activation. Provider/class, supported reasoning, exact source
fingerprint, suite/status/timestamp and identity are checked. Manual pins remain
fixed. Claude Agent alias schema is honored using verified env mappings.

Live evidence from ~/.claude/state/model_auto_update.json:

- trivial/recon: gpt-5.6-luna -> gpt-6-luna, PASS.
- medium: gpt-5.6-terra -> gpt-6-sol, PASS.
- coding/hard/lead: claude-opus-5 baseline, PASS; installed Claude 2.1.276 alias
  currently resolves to Opus 5. A future recommended alias needs a current CLI.
- consilium_bio: gpt-6-astra remains current.
- high_blast_radius: Sonnet baseline qualification failed; existing
  claude-sonnet-5 preserved and alias frozen to that ID without verified claim.
- PAL/Z.ai/Grok: held with explicit unsupported-class status. No cross-provider
  discovery/entitlement substitution is implemented.

Live ordinary codex_routed_worker.py medium --ephemeral --sandbox read-only
completed successfully. CLI header and worker receipt both reported gpt-6-sol,
medium reasoning, source=balancer, requested_model=effective_model. A catalog
refresh timed out and an unrelated Context7 MCP auth warning appeared on this
run, but the worker completed on the selected model (no fallback). Earlier live
app-server catalog discovery and exact candidate qualification had succeeded.

Validation:

- 76 pytest cases across model_auto_update, model_update_consumers,
  codex_adapter_exact_route_contract, codex_capability_routing, gpt56_routing.
- test_model_balancer_all.sh: 7/7 suites PASS after initial installation.
- Consumer agent separately verified 26 enforcer and 18 telemetry assertions.
- Installed/template byte parity verified for all five modified/new scripts.
- git diff --check PASS; installed start command used fresh daily state without
  relaunching qualification; startup summary contains actual check results.
- Independent review found and closed invalid-receipt settings publication;
  regression cases prove rejected receipts cannot advance Claude aliases.

Backup: ~/.claude/backups/model-auto-update-20260927T091002Z.
Main-session Claude model setting preserved. Restart existing Claude sessions so
Agent aliases inherit the newly pinned mappings. No repository commit/push made.
