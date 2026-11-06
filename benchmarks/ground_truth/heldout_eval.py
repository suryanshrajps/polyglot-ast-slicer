"""Runs the Python (Jedi) and TS/JS (tsc) ground-truth harnesses on held-out repos.

The held-out repositories are never used for tuning. Their numbers are written to
``metrics/heldout_ground_truth.json`` and rendered into README/METRICS.

    python benchmarks/ground_truth/heldout_eval.py --repos-dir /tmp/heldout
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS = ROOT / "metrics" / "heldout_ground_truth.json"

HELDOUT = {
    "werkzeug": {
        "url": "https://github.com/pallets/werkzeug.git",
        "sha": "6604c3b8784ecc7cf5ac0b7412d916989b4c7d45",
        "language": "python",
    },
    "zustand": {
        "url": "https://github.com/pmndrs/zustand.git",
        "sha": "9fed2eb2f3af672da9f7bf694b996df3831fc207",
        "language": "typescript",
        "path": ".",
        "root": "src",
        "exclude": "__tests__,tests",
    },
    "moment": {
        "url": "https://github.com/moment/moment.git",
        "sha": "485d9a7d709bd5f3869a7ad24630cf0746d072dc",
        "language": "javascript",
        "path": ".",
        "root": "src",
        "exclude": "test",
    },
}


def ensure_repo(name: str, repos_dir: Path) -> Path:
    config = HELDOUT[name]
    target = repos_dir / name
    if not target.exists():
        repos_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", config["url"], str(target)], check=True)
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if head != config["sha"]:
        subprocess.run(
            ["git", "-C", str(target), "fetch", "-q", "origin", config["sha"]], check=False
        )
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-q", config["sha"]], check=True
        )
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    if head != config["sha"]:
        raise SystemExit(f"{target} is at {head}, expected {config['sha']}")
    return target


def run_json(*args: str) -> dict:
    completed = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos-dir", type=Path, default=Path("/tmp/heldout"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results: dict[str, dict] = {}
    for name, config in HELDOUT.items():
        repo = ensure_repo(name, args.repos_dir)
        if config["language"] == "python":
            summary = run_json(
                sys.executable,
                "benchmarks/ground_truth/python_eval.py",
                str(repo),
                "--json",
            )
        else:
            summary = run_json(
                "node",
                "benchmarks/ground_truth/ts_eval.mjs",
                str(repo / str(config["path"])),
                "--name",
                name,
                "--root",
                str(config["root"]),
                "--exclude",
                str(config["exclude"]),
                "--python",
                sys.executable,
            )
            summary = summary["groups"]["ts" if config["language"] == "typescript" else "js"]
        results[name] = {
            "language": config["language"],
            "sha": config["sha"],
            "sites": summary["project_sites"],
            "correct": summary["correct"],
            "wrong": summary["wrong"],
            "missed": summary["missed"],
            "ambiguous": summary["ambiguous"],
            "ambiguous_but_reported": summary["ambiguous_but_reported"],
            "precision": summary["precision"],
            "precision_ci95": summary["precision_ci95"],
            "high_confidence": summary["high_confidence"],
            "recall": summary["recall"],
            "recall_ci95": summary["recall_ci95"],
        }

    payload = {
        "pinned": {name: config["sha"] for name, config in HELDOUT.items()},
        "scope": {
            name: {
                "root": config.get("root", "src"),
                "exclude": config.get("exclude", ""),
            }
            for name, config in HELDOUT.items()
        },
        "results": results,
    }
    METRICS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload))
        return
    for name, row in results.items():
        print(
            f"{name}: sites={row['sites']} correct={row['correct']} wrong={row['wrong']} "
            f"missed={row['missed']} precision={row['precision']:.4f} "
            f"recall={row['recall']:.4f}"
        )
    print(f"wrote {METRICS}")


if __name__ == "__main__":
    main()
