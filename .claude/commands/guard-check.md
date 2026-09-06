Review the current diff (uncommitted changes, or the changes introduced in
this session if nothing is staged) against the project's non-negotiables.

Steps:
1. Re-read `CLAUDE.md`'s "Prime directive" and "Non-negotiable hard gates"
   sections, and `docs/success-criteria.md` in full.
2. For each changed file, check:
   - Does it still trace back to one of the four phases (Resolve/Crawl/Prove/
     Refresh)? If not, flag it explicitly as possible scope creep.
   - Does it touch anything covered by a hard gate (precision matching logic,
     budget governor, snapshot writing, output schema validation)? If so, is
     there a corresponding test change, per `docs/testing-strategy.md`?
   - Does it introduce any code path that could overwrite or delete a prior
     snapshot, skip budget checks, or emit a claim without an evidence state?
     These are automatic blockers — call them out clearly, don't soften them.
3. Produce a short report: what changed, which hard gates it touches (if any),
   whether tests cover it, and any scope-creep or gate-risk flags. If nothing
   is wrong, say so plainly rather than inventing caveats.
4. Do not silently fix issues you find here — report them, and only fix if the
   user confirms (unless the fix is trivially within the original task's scope).
