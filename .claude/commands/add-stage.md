Scaffold a new pipeline stage named "$ARGUMENTS".

Steps:
1. Read `docs/component-specs.md` in full first — a new stage must follow the
   same contract shape as the existing ones (explicit input type, explicit
   output type, an explicit list of "must" and "must not" behaviors, and a list
   of tests to write). If this stage doesn't fit the Resolve/Crawl/Extract/
   Verify/Assemble/Persist shape described in `docs/approach.md`, stop and ask
   the user whether it really belongs in this pipeline before creating it —
   check for scope creep against `docs/problem-statement.md`'s four phases.
2. Add a new section to `docs/component-specs.md` for this stage, following the
   exact same structure as the existing entries (Responsibility / Input /
   Output / Must / Must not / Tests to write).
3. Create `src/pipeline/$ARGUMENTS.py` (or the correct directory if this isn't
   a pipeline stage — e.g. `src/storage/` or `src/orchestrator/`) with a
   docstring that restates the contract from step 2, typed function
   signatures, and `NotImplementedError` bodies — do not invent behavior beyond
   what's documented.
4. Create the matching `tests/.../test_$ARGUMENTS.py` with stub test functions
   for every case listed in the "Tests to write" section — these should fail
   or be skipped until implemented, not silently pass.
5. Update `docs/folder-structure.md` if the new file changes the tree.
6. Do not wire the new stage into `src/orchestrator/runner.py` in this same
   step unless explicitly asked — scaffolding and integration are separate,
   reviewable changes.
