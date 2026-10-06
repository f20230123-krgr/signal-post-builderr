"""
Build the hosted viewer site (served by GitHub Pages) from finished runs.

    python -m src.build_site --corpus results --smoke path/to/smoke-run --out site

`--corpus` is the full run (envelopes.jsonl + run-report.json); it becomes the site root.
`--smoke` is the 100-company smoke-test run Builderr asks every public artifact to
include; it is served under /smoke/ together with its raw envelopes and run report, so a
reviewer can open the viewer, or download exactly what the agent emitted.

Nothing here invents content: the pages are written from the envelopes as they are.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.reporting import write_site


def _load(run_dir: Path) -> tuple[list[dict], dict, datetime]:
    envelopes = [json.loads(line) for line in (run_dir / "envelopes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    report_path = run_dir / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    generated_at = datetime.fromtimestamp((run_dir / "envelopes.jsonl").stat().st_mtime, tz=timezone.utc)
    return envelopes, report, generated_at


_RAW_FILES = (
    ("envelopes.jsonl", "Envelopes (JSONL, one per company)"),
    ("run-report.json", "Run report (requests, time, cost)"),
    ("manifest.txt", "Organisation numbers processed"),
)


def _copy_raw(run_dir: Path, dest: Path) -> list[dict]:
    """Copy a run's raw output next to its page; return the download links for the viewer."""
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for name, label in _RAW_FILES:
        if (run_dir / name).exists():
            shutil.copy(run_dir / name, dest / name)
            files.append({"label": label, "href": name, "bytes": (dest / name).stat().st_size})
    return files


def build(corpus: Path, smoke: Path | None, out: Path) -> dict:
    """The smoke test (100 companies, opens at once) is the site root when there is one;
    the full corpus lives under /corpus/. Each page links to the other."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / ".nojekyll").write_text("", encoding="utf-8")  # serve files as they are, no Jekyll pass

    result = {}
    envelopes, report, when = _load(corpus)
    corpus_dir = out / "corpus" if smoke is not None else out
    to_smoke = [{"label": "100-company smoke test", "href": "../"}] if smoke is not None else []
    corpus_files = _copy_raw(corpus, corpus_dir)
    result["corpus"] = write_site(
        corpus_dir, envelopes, when, search_keys=report.get("search_keys"), run_id=report.get("run_id"),
        title=f"Full run: {len(envelopes):,} companies", links=to_smoke, downloads=corpus_files,
    )

    if smoke is not None:
        s_env, s_report, s_when = _load(smoke)
        smoke_files = _copy_raw(smoke, out)
        result["smoke"] = write_site(
            out, s_env, s_when, search_keys=s_report.get("search_keys"), run_id=s_report.get("run_id"),
            title=f"Smoke test: {len(s_env)} companies",
            links=[{"label": f"Full run ({len(envelopes):,})", "href": "corpus/"}], downloads=smoke_files,
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the hosted Signalpost Atlas site.")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--smoke", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("site"))
    args = parser.parse_args()
    print(json.dumps(build(args.corpus, args.smoke, args.out), indent=2))


if __name__ == "__main__":
    main()
