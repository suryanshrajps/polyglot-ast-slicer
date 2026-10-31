"""Regenerates METRICS.md and the committed result artifacts.

    make metrics          # network: clones pinned repos, reruns harnesses, rewrites artifacts
    make metrics-check    # offline: renders METRICS.md from committed artifacts and diffs

The commit-time gate never clones or runs the harnesses.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = ROOT / "metrics"
PINNED = {
    "requests": ("https://github.com/psf/requests.git", "3c7baaa05ab4aa49cc819ce3a819d260de19c157"),
    "click": ("https://github.com/pallets/click.git", "2247b35ea1c47c727d7a06e51fa280e12a863ff6"),
}
SCALE_FILES = 5000
LABEL_REPOS = ("requests", "click", "zod")


def wilson(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def hand_metrics() -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for repo in LABEL_REPOS:
        path = ROOT / "labels" / repo / "labels.csv"
        if not path.exists():
            continue
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        verdicts = [row["verdict"] for row in rows if row["verdict"]]
        if verdicts:
            result[repo] = {
                "correct": sum(1 for verdict in verdicts if verdict == "correct"),
                "incorrect": sum(1 for verdict in verdicts if verdict == "incorrect"),
                "unsure": sum(1 for verdict in verdicts if verdict == "unsure"),
                "total": len(verdicts),
            }
    return result


def run_json(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def ensure_repo(name: str, repos_dir: Path) -> Path:
    target = repos_dir / name
    url, sha = PINNED[name]
    if not target.exists():
        repos_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", url, str(target)], check=True)
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if head != sha:
        subprocess.run(["git", "-C", str(target), "fetch", "-q", "origin", sha], check=False)
        subprocess.run(["git", "-C", str(target), "checkout", "-q", sha], check=True)
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    if head != sha:
        raise SystemExit(f"{target} is at {head}, expected {sha}")
    return target


def collect(repos_dir: Path) -> dict[str, object]:
    data: dict[str, object] = {"precision": {}, "recall": {}, "synthetic": {}, "scale": {}}
    for name in PINNED:
        repo = ensure_repo(name, repos_dir)
        precision = run_json("benchmarks/edge_eval/precision.py", str(repo), "100", "--json")
        recall = run_json("benchmarks/edge_eval/recall.py", str(repo), "150", "--json")
        precision.pop("repo", None)
        recall.pop("repo", None)
        data["precision"][name] = precision  # type: ignore[index]
        data["recall"][name] = recall  # type: ignore[index]

    benchmark = run_json("benchmarks/token_bench.py", "--json")
    data["synthetic"] = {  # type: ignore[assignment]
        "files": benchmark["index"]["cold"]["files_indexed"],
        "symbols": benchmark["index"]["cold"]["total_symbols"],
        "edges": benchmark["index"]["cold"]["total_edges"],
        "slices": [
            {
                "qualified_name": row["qualified_name"],
                "touched_files_tokens": row["touched_files_tokens"],
                "full_repo_tokens": row["full_repo_tokens"],
                "slice_tokens": row["slice_tokens"],
                "saved_vs_files_pct": row["saved_vs_files_pct"],
            }
            for row in benchmark["slices"]
        ],
    }
    scale = run_json("benchmarks/scale/scale_bench.py", "--files", str(SCALE_FILES), "--json")
    data["scale"] = {key: scale[key] for key in ("files", "symbols", "edges")}  # type: ignore[assignment]
    return data


def load_artifacts() -> dict[str, object]:
    data: dict[str, object] = {"precision": {}, "recall": {}, "synthetic": {}, "scale": {}}
    for name in PINNED:
        data["precision"][name] = json.loads(  # type: ignore[index]
            (METRICS_DIR / f"{name}_precision.json").read_text(encoding="utf-8")
        )
        data["recall"][name] = json.loads(  # type: ignore[index]
            (METRICS_DIR / f"{name}_recall.json").read_text(encoding="utf-8")
        )
    data["synthetic"] = json.loads((METRICS_DIR / "synthetic_benchmark.json").read_text())
    data["scale"] = json.loads((METRICS_DIR / "scale_5000.json").read_text())
    return data


def fmt(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def render(data: dict[str, object]) -> str:
    precision = data["precision"]  # type: ignore[assignment]
    recall = data["recall"]  # type: ignore[assignment]
    synthetic = data["synthetic"]  # type: ignore[assignment]
    scale = data["scale"]  # type: ignore[assignment]

    lines: list[str] = [
        "# Generated metrics",
        "",
        "<!-- Generated by `make metrics`. Do not edit by hand. -->",
        "",
        "Pinned inputs: "
        + ", ".join(f"{name} `{sha[:7]}`" for name, (_, sha) in PINNED.items())
        + ". Intervals are 95% Wilson score intervals.",
        "",
        "## Definitions",
        "",
        "- **Confirmed by Jedi (strict)** — Jedi's definition lands inside the claimed target's",
        "  file and line span.",
        "- **Not contradicted by Jedi (canonical)** — strict, or Jedi lands on a file that",
        "  re-exports the claimed symbol. This is not precision and is not the headline.",
        "- **Hand-labeled precision (headline)** — human verdict on the blind 100-edge packet.",
        "  **UNMEASURED**: no labels have been committed.",
        "- **Conditional recall** — found edges divided by call sites Jedi resolves to a project",
        "  symbol. **Unconditional recall is UNMEASURED.**",
        "- CommonJS JavaScript edges are not measured: `require()`/`module.exports` are not",
        "  resolved, so an express sample cannot reach 100 edges.",
        "",
        "## Hand-labeled precision (headline)",
        "",
        "| Repo | Labeled | Correct | Unsure | Precision (unsure=incorrect) | "
        "Precision (unsure excluded) |",
        "|---|---:|---:|---:|---|---|",
    ]
    hand = hand_metrics()
    for repo in LABEL_REPOS:
        entry = hand.get(repo)
        if entry is None:
            lines.append(f"| `{repo}` | 0 | — | — | UNMEASURED | UNMEASURED |")
            continue
        total, correct, unsure = entry["total"], entry["correct"], entry["unsure"]
        low, high = wilson(correct, total)
        decided = correct + entry["incorrect"]
        low2, high2 = wilson(correct, decided)
        lines.append(
            f"| `{repo}` | {total} | {correct} | {unsure} | "
            f"{fmt(correct / total)} (95% CI {fmt(low)}–{fmt(high)}) | "
            f"{fmt(correct / decided) if decided else 'n/a'} "
            f"(95% CI {fmt(low2)}–{fmt(high2)}) |"
        )
    lines += [
        "",
        "## Canary check (seeded wrong edges mixed into the blind set)",
        "",
        "| Repo | Seeded | Labeled correct | False-accept rate | False rejects | "
        "Unsure | Agreement with prior pass | Cohen's kappa |",
        "|---|---:|---:|---|---:|---:|---|---|",
    ]
    for repo in LABEL_REPOS:
        canary_path = ROOT / "labels" / repo / "canary_result.json"
        if not canary_path.exists():
            lines.append(f"| `{repo}` | — | — | UNMEASURED | — | — | — | — |")
            continue
        canary = json.loads(canary_path.read_text(encoding="utf-8"))
        if canary.get("prior_pass") and canary["kappa"] is not None:
            kappa_text = f"{canary['kappa']:.3f}"
        else:
            kappa_text = "n/a"
        agreement = (
            f"{canary['agreement_with_original']}/{canary['real']}"
            if canary.get("prior_pass")
            else "n/a (no prior pass)"
        )
        lines.append(
            f"| `{repo}` | {canary['seeded']} | {canary['seeded_labeled_correct']} | "
            f"{fmt(canary['false_accept_rate'])} "
            f"(95% CI {fmt(canary['false_accept_ci95'][0])}–"
            f"{fmt(canary['false_accept_ci95'][1])}) | "
            f"{canary['real_labeled_incorrect']}/{canary['real']} | {canary['unsure']} | "
            f"{agreement} | {kappa_text} |"
        )
    lines += [
        "",
        "## Hard-kind precision (v2 packets)",
        "",
        "| Repo | Kind | n | Correct | Unsure | Precision (unsure=incorrect) |",
        "|---|---|---:|---:|---:|---|",
    ]
    sys.path.insert(0, str(ROOT / "benchmarks" / "edge_eval"))
    from score_labels import load_labels, score  # noqa: E402

    label_dirs = {"requests": "requests_hard", "click": "click_hard", "zod": "zod"}
    for repo, directory in label_dirs.items():
        label_path = ROOT / "labels" / directory / "hard_labels.csv"
        key_path = ROOT / "labels" / directory / "v2_answer_key.json"
        if not label_path.exists() or not key_path.exists():
            lines.append(f"| `{repo}` | all | — | — | — | UNMEASURED |")
            continue
        labels = load_labels(label_path)
        if not labels:
            lines.append(f"| `{repo}` | all | — | — | — | UNMEASURED |")
            continue
        key = json.loads(key_path.read_text(encoding="utf-8"))
        result = score(labels, key)
        for kind, entry in result["per_kind"].items():
            low, high = entry["precision_ci95_inclusive"]
            lines.append(
                f"| `{repo}` | `{kind}` | {entry['n']} | {entry['correct']} | "
                f"{entry['unsure']} | {fmt(entry['precision_unsure_incorrect'])} "
                f"(95% CI {fmt(low)}–{fmt(high)}) |"
            )
        hard = result["hard_edges"]
        low, high = hard["precision_ci95_inclusive"]
        lines.append(
            f"| `{repo}` | **hard edges** | {hard['n']} | {hard['correct']} | "
            f"{hard['unsure']} | {fmt(hard['precision_unsure_incorrect'])} "
            f"(95% CI {fmt(low)}–{fmt(high)}) |"
        )
    lines += [
        "",
        "## Edge agreement with Jedi (100 random call edges, seed 42)",
        "",
        "| Repo | Sampled | Confirmed (strict) | Not contradicted (canonical) |",
        "|---|---:|---:|---:|",
    ]
    for name, entry in precision.items():
        low, high = entry["precision_ci95"]
        lines.append(
            f"| `{name}` | {entry['sampled']} | {entry['strict']}/{entry['sampled']} | "
            f"{entry['canonical']}/{entry['sampled']} "
            f"({fmt(entry['precision'])}; 95% CI {fmt(low)}–{fmt(high)}) |"
        )
    lines += [
        "",
        "The canonical column also accepts re-export hops; it is not hand-labeled precision.",
        "",
        "## Conditional recall (150 call sites, seed 7; Jedi-resolvable only)",
        "",
        "| Repo | Found / resolvable | Recall | Unresolved by Jedi |",
        "|---|---:|---:|---:|",
    ]
    for name, entry in recall.items():
        low, high = entry["recall_ci95"]
        lines.append(
            f"| `{name}` | {entry['tool_edges_present']}/{entry['jedi_project_targets']} | "
            f"{fmt(entry['recall'])} (95% CI {fmt(low)}–{fmt(high)}) | "
            f"{entry['unresolved_by_jedi']} |"
        )
    lines += [
        "",
        "Unconditional recall (hand-labelled true targets) is UNMEASURED.",
        "",
        "## Synthetic token benchmark (hops=2)",
        "",
        "| Target | Touched files | Repo | Slice | Saved vs files |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in synthetic["slices"]:
        lines.append(
            f"| `{row['qualified_name']}` | {row['touched_files_tokens']} | "
            f"{row['full_repo_tokens']} | {row['slice_tokens']} | "
            f"{row['saved_vs_files_pct']}% |"
        )
    lines += [
        "",
        f"Corpus: {synthetic['files']} files, {synthetic['symbols']} symbols, "
        f"{synthetic['edges']} edges. Latency is machine-dependent and omitted here; "
        "run `benchmarks/scale/scale_bench.py` for timings.",
        "",
        f"## Scale ({scale['files']} generated files, seed 42)",
        "",
        "| Files | Symbols | Edges |",
        "|---:|---:|---:|",
        f"| {scale['files']} | {scale['symbols']} | {scale['edges']} |",
        "",
        "Larger runs: `python benchmarks/scale/scale_bench.py --files 50000 --json`.",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(data: dict[str, object]) -> None:
    METRICS_DIR.mkdir(exist_ok=True)
    for name in PINNED:
        (METRICS_DIR / f"{name}_precision.json").write_text(
            json.dumps(data["precision"][name], indent=2) + "\n",  # type: ignore[index]
            encoding="utf-8",
        )
        (METRICS_DIR / f"{name}_recall.json").write_text(
            json.dumps(data["recall"][name], indent=2) + "\n",  # type: ignore[index]
            encoding="utf-8",
        )
    (METRICS_DIR / "synthetic_benchmark.json").write_text(
        json.dumps(data["synthetic"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "scale_5000.json").write_text(
        json.dumps(data["scale"], indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline: verify METRICS.md")
    parser.add_argument("--render", action="store_true", help="offline: rewrite METRICS.md")
    parser.add_argument("--repos-dir", type=Path, default=Path("/tmp/eval-repos"))
    args = parser.parse_args()

    if args.render:
        (ROOT / "METRICS.md").write_text(render(load_artifacts()), encoding="utf-8")
        print("rewrote METRICS.md from artifacts and labels")
        return

    if args.check:
        rendered = render(load_artifacts())
        current = (ROOT / "METRICS.md").read_text(encoding="utf-8")
        if current != rendered:
            diff = difflib.unified_diff(
                current.splitlines(), rendered.splitlines(), fromfile="METRICS.md", lineterm=""
            )
            print("\n".join(list(diff)[:200]))
            raise SystemExit("METRICS.md does not match committed artifacts")
        print("METRICS.md matches committed artifacts")
        return

    data = collect(args.repos_dir)
    write_artifacts(data)
    (ROOT / "METRICS.md").write_text(render(data), encoding="utf-8")
    print("wrote METRICS.md and metrics/*.json")


if __name__ == "__main__":
    main()
