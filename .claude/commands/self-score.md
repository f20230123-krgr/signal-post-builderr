Run the local scoring harness (`src/scoring/self_check.py`) against the
hand-labeled fixtures in `fixtures/`.

Steps:
1. Read `docs/testing-strategy.md` section 5 and `docs/success-criteria.md` to
   confirm the current thresholds (coverage ≥21/35, recall ≥60%, precision ≥95%).
2. Run the self-check harness (e.g. `python -m src.scoring.self_check
   --fixtures fixtures/`). If it doesn't exist yet, say so plainly and stop —
   do not fabricate numbers.
3. Report the resulting coverage, weighted company recall, and external
   precision numbers exactly as computed, with no rounding that would hide a
   near-miss (e.g. report 94.6%, not "~95%").
4. State clearly, in one line, whether each hard gate currently passes or
   fails. If any fails, do not describe the work as "done" anywhere in your
   response — say what's failing and by how much instead.
5. Do not modify the thresholds in code or docs to make a run "pass." If a
   threshold seems wrong, that's a separate conversation with the user, not
   something to quietly adjust here.
