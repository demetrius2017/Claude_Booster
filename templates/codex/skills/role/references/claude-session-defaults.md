<!-- BEGIN BOOSTER SESSION ROLE -->
Default execution mode for a new conversation is `developer`: implement and
integrate directly; delegate useful bounded research and independent testing.
Immediate code inspection, builds, tests, and debugging may be performed directly.
Do not create helpers without useful work. The main agent owns the whole result.
Use `/role developer`, `/role lead`, or `/role status` to select/inspect the mode;
read `~/.claude/commands/role.md` directly, without the general Booster runner.
Retain the mode and reusable role-agent IDs within this conversation, including
compaction/resume. New conversations start in developer mode.
This role selection replaces legacy blanket always-Lead/always-delegate cues in
Booster memory and phase prompts for ordinary implementation. Permission checks,
data protections, verification and phase transitions remain applicable; report
an incompatible hook block instead of bypassing it. Explicitly requested command
contracts apply to their tasks. Keep hooks and memory access configured as they are.
Before code edits, complete proportionate recon/planning and advance the project
phase with `python3 ~/.claude/scripts/phase.py set IMPLEMENT`. Selecting `/role`
alone does not change phase. Resolve phase blocks through the normal transition.
<!-- END BOOSTER SESSION ROLE -->
