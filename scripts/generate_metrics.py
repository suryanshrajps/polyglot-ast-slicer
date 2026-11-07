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
import os
import re
import subprocess
import sys
from pathlib import Path

from polyglot_ast_slicer.parser import EXTENSION_TO_LANGUAGE

ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = ROOT / "metrics"

PINNED = {
    "requests": ("https://github.com/psf/requests.git", "3c7baaa05ab4aa49cc819ce3a819d260de19c157"),
    "click": ("https://github.com/pallets/click.git", "2247b35ea1c47c727d7a06e51fa280e12a863ff6"),
}
GROUND_TRUTH_PINNED = {
    **PINNED,
    "flask": ("https://github.com/pallets/flask.git", "49943b5a94413a530761350f04e896f76e8d620e"),
}
TS_GROUND_TRUTH = {
    "zod": {
        "url": "https://github.com/colinhacks/zod.git",
        "sha": "ca42965df46b2f7e2747db29c40a26bcb32a51d5",
        "path": ".",
        "root": "src",
        "exclude": "__tests__,benchmarks",
    },
    "nest": {
        "url": "https://github.com/nestjs/nest.git",
        "sha": "416830c3924b37ec354d4e15c14119519e389afc",
        "path": ".",
        "root": "packages",
        "exclude": "test,.spec.ts,.e2e-spec.ts",
    },
    "bulletproof-react": {
        "url": "https://github.com/alan2207/bulletproof-react.git",
        "sha": "9506629ed003a561c6627735480cce4994244bb4",
        "path": "apps/react-vite",
        "root": "src",
        "exclude": "__tests__,test,testing,.test.ts,.test.tsx,.stories.ts,.stories.tsx",
    },
    "node-fetch": {
        "url": "https://github.com/node-fetch/node-fetch.git",
        "sha": "8b3320d2a7c07bce4afc6b2bf6c3bbddda85b01f",
        "path": ".",
        "root": "src",
    },
    "express": {
        "url": "https://github.com/expressjs/express.git",
        "sha": "1faf228935aa0a13111f92c28ee795be64ce3f0f",
        "path": ".",
        "root": "lib",
    },
}
TS_FORMS = (
    "bare",
    "module_attr",
    "self_cls",
    "super",
    "constructor",
    "default_import",
    "aliased_import",
    "chained",
    "callback",
    "jsx",
    "other",
)
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


def run_node_json(*args: str) -> dict:
    completed = subprocess.run(
        ["node", *args, "--python", sys.executable],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def ensure_repo(
    name: str,
    repos_dir: Path,
    pinned: dict[str, tuple[str, str]] | None = None,
) -> Path:
    target = repos_dir / name
    url, sha = (pinned or PINNED)[name]
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


def collected_test_count() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts="],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"pytest collection failed:\n{completed.stderr[-2000:]}")
    match = re.search(r"(\d+) tests? collected", completed.stdout)
    if match is None:
        raise SystemExit("could not parse pytest collection output")
    return int(match.group(1))


def collect_ts_ground_truth(repos_dir: Path) -> dict[str, object]:
    repos: dict[str, dict] = {}
    for name, config in TS_GROUND_TRUTH.items():
        repo = ensure_repo(name, repos_dir, {name: (config["url"], config["sha"])})
        args = [
            "benchmarks/ground_truth/ts_eval.mjs",
            str(repo / str(config["path"])),
            "--name",
            name,
            "--root",
            str(config["root"]),
        ]
        if config.get("exclude"):
            args += ["--exclude", str(config["exclude"])]
        summary = run_node_json(*args)
        summary.pop("unjudged_sample", None)
        summary.pop("repo", None)
        repos[name] = summary

    groups: dict[str, dict] = {}
    for group in ("ts", "js"):
        totals = {
            "sites": 0,
            "project_sites": 0,
            "correct": 0,
            "wrong": 0,
            "missed": 0,
            "ambiguous": 0,
            "ambiguous_but_reported": 0,
            "hc_claims": 0,
            "hc_correct": 0,
            "hc_wrong": 0,
            "by_form": {},
        }
        for entry in repos.values():
            source = entry["groups"][group]
            if int(source["project_sites"]) == 0:
                continue
            for key in (
                "sites",
                "project_sites",
                "correct",
                "wrong",
                "missed",
                "ambiguous",
                "ambiguous_but_reported",
            ):
                totals[key] += int(source[key])
            totals["hc_claims"] += int(source["high_confidence"]["claims"])
            totals["hc_correct"] += int(source["high_confidence"]["correct"])
            totals["hc_wrong"] += int(source["high_confidence"]["wrong"])
            for form in TS_FORMS:
                frow = source["by_form"].get(form)
                if not frow or not frow["sites"]:
                    continue
                fagg = totals["by_form"].setdefault(
                    form,
                    {
                        "sites": 0,
                        "correct": 0,
                        "wrong": 0,
                        "missed": 0,
                        "ambiguous_but_reported": 0,
                    },
                )
                for key in fagg:
                    fagg[key] += int(frow[key])
        if totals["project_sites"] == 0:
            continue
        correct, wrong = totals["correct"], totals["wrong"]
        hc_correct, hc_wrong = totals["hc_correct"], totals["hc_wrong"]
        by_form = {}
        for form, frow in totals["by_form"].items():
            form_wrong = frow["wrong"]
            form_hit = frow["correct"]
            by_form[form] = {
                **frow,
                "precision": form_hit / (form_hit + form_wrong) if form_hit + form_wrong else 0.0,
                "precision_ci95": wilson(form_hit, form_hit + form_wrong),
                "recall": form_hit / frow["sites"] if frow["sites"] else 0.0,
                "recall_ci95": wilson(form_hit, frow["sites"]),
            }
        groups[group] = {
            "sites": totals["sites"],
            "project_sites": totals["project_sites"],
            "correct": correct,
            "wrong": wrong,
            "missed": totals["missed"],
            "ambiguous": totals["ambiguous"],
            "ambiguous_but_reported": totals["ambiguous_but_reported"],
            "precision": correct / (correct + wrong) if correct + wrong else 0.0,
            "precision_ci95": wilson(correct, correct + wrong),
            "high_confidence": {
                "threshold": 0.95,
                "claims": totals["hc_claims"],
                "correct": hc_correct,
                "wrong": hc_wrong,
                "precision": hc_correct / (hc_correct + hc_wrong)
                if hc_correct + hc_wrong
                else 0.0,
                "precision_ci95": wilson(hc_correct, hc_correct + hc_wrong),
            },
            "recall": correct / totals["project_sites"] if totals["project_sites"] else 0.0,
            "recall_ci95": wilson(correct, totals["project_sites"]),
            "by_form": by_form,
        }

    existing_audit: dict = {}
    artifact = METRICS_DIR / "ts_ground_truth.json"
    if artifact.exists():
        existing_audit = json.loads(artifact.read_text(encoding="utf-8")).get("hand_audit", {})
    return {
        "pinned": {name: config["sha"] for name, config in TS_GROUND_TRUTH.items()},
        "scope": {
            name: {"root": config["root"], "exclude": config.get("exclude", "")}
            for name, config in TS_GROUND_TRUTH.items()
        },
        "typescript": next(iter(repos.values()))["typescript"] if repos else "",
        "repos": repos,
        "groups": groups,
        "hand_audit": existing_audit,
    }


def collect(repos_dir: Path) -> dict[str, object]:
    data: dict[str, object] = {
        "precision": {},
        "recall": {},
        "synthetic": {},
        "scale": {},
        "ground_truth": {},
        "ts_ground_truth": {},
        "size_comparison": {},
        "heldout": {},
        "examples": {},
        "performance": {},
        "tests": {},
    }
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

    repos: dict[str, dict] = {}
    for name in GROUND_TRUTH_PINNED:
        repo = ensure_repo(name, repos_dir, GROUND_TRUTH_PINNED)
        summary = run_json(
            "benchmarks/ground_truth/python_eval.py",
            str(repo),
            "--json",
            "--unjudged",
            "10",
        )
        summary.pop("unjudged_sample", None)
        summary.pop("repo", None)
        repos[name] = summary
    sites = sum(int(item["project_sites"]) for item in repos.values())
    correct = sum(int(item["correct"]) for item in repos.values())
    wrong = sum(int(item["wrong"]) for item in repos.values())
    hc_claims = sum(int(item["high_confidence"]["claims"]) for item in repos.values())
    hc_correct = sum(int(item["high_confidence"]["correct"]) for item in repos.values())
    hc_wrong = sum(int(item["high_confidence"]["wrong"]) for item in repos.values())
    forms: dict[str, dict[str, int]] = {}
    for item in repos.values():
        for form, row in item["by_form"].items():
            aggregate = forms.setdefault(
                form,
                {
                    "sites": 0,
                    "correct": 0,
                    "wrong": 0,
                    "missed": 0,
                    "ambiguous_but_reported": 0,
                },
            )
            for key in aggregate:
                aggregate[key] += int(row[key])
    existing_audit: dict = {}
    artifact = METRICS_DIR / "python_ground_truth.json"
    if artifact.exists():
        existing_audit = json.loads(artifact.read_text(encoding="utf-8")).get("hand_audit", {})
    data["ground_truth"] = {  # type: ignore[assignment]
        "pinned": {name: pinned[1] for name, pinned in GROUND_TRUTH_PINNED.items()},
        "repos": repos,
        "combined": {
            "project_sites": sites,
            "correct": correct,
            "wrong": wrong,
            "missed": sites - correct - wrong,
            "ambiguous": sum(int(item["ambiguous"]) for item in repos.values()),
            "ambiguous_but_reported": sum(
                int(item["ambiguous_but_reported"]) for item in repos.values()
            ),
            "precision": correct / (correct + wrong) if correct + wrong else 0.0,
            "recall": correct / sites if sites else 0.0,
            "high_confidence": {
                "threshold": 0.95,
                "claims": hc_claims,
                "correct": hc_correct,
                "wrong": hc_wrong,
                "precision": hc_correct / (hc_correct + hc_wrong)
                if hc_correct + hc_wrong
                else 0.0,
            },
            "by_form": forms,
        },
        "hand_audit": existing_audit,
    }
    data["ts_ground_truth"] = collect_ts_ground_truth(repos_dir)
    data["size_comparison"] = run_json(
        "benchmarks/size_compare.py", "--json", "--repos-dir", str(repos_dir)
    )
    heldout_dir = Path(os.environ.get("POLYGLOT_AST_HELDOUT_DIR", "/tmp/heldout"))
    data["heldout"] = run_json(
        "benchmarks/ground_truth/heldout_eval.py",
        "--json",
        "--repos-dir",
        str(heldout_dir),
    )
    subprocess.run(
        [sys.executable, "benchmarks/readme_examples.py"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    data["examples"] = json.loads((METRICS_DIR / "examples.json").read_text(encoding="utf-8"))
    data["performance"] = json.loads(
        (ROOT / "verify" / "results" / "scale.json").read_text(encoding="utf-8")
    )
    data["tests"] = {"count": collected_test_count()}
    return data


def load_artifacts() -> dict[str, object]:
    data: dict[str, object] = {
        "precision": {},
        "recall": {},
        "synthetic": {},
        "scale": {},
        "ground_truth": {},
        "ts_ground_truth": {},
        "size_comparison": {},
        "heldout": {},
        "examples": {},
        "performance": {},
        "tests": {},
    }
    for name in PINNED:
        data["precision"][name] = json.loads(  # type: ignore[index]
            (METRICS_DIR / f"{name}_precision.json").read_text(encoding="utf-8")
        )
        data["recall"][name] = json.loads(  # type: ignore[index]
            (METRICS_DIR / f"{name}_recall.json").read_text(encoding="utf-8")
        )
    data["synthetic"] = json.loads((METRICS_DIR / "synthetic_benchmark.json").read_text())
    data["scale"] = json.loads((METRICS_DIR / "scale_5000.json").read_text())
    data["ground_truth"] = json.loads(
        (METRICS_DIR / "python_ground_truth.json").read_text(encoding="utf-8")
    )
    data["ts_ground_truth"] = json.loads(
        (METRICS_DIR / "ts_ground_truth.json").read_text(encoding="utf-8")
    )
    data["size_comparison"] = json.loads(
        (METRICS_DIR / "size_comparison.json").read_text(encoding="utf-8")
    )
    data["heldout"] = json.loads(
        (METRICS_DIR / "heldout_ground_truth.json").read_text(encoding="utf-8")
    )
    data["examples"] = json.loads((METRICS_DIR / "examples.json").read_text(encoding="utf-8"))
    data["performance"] = json.loads(
        (ROOT / "verify" / "results" / "scale.json").read_text(encoding="utf-8")
    )
    data["tests"] = json.loads((METRICS_DIR / "tests.json").read_text(encoding="utf-8"))
    return data


def fmt(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def render(data: dict[str, object]) -> str:
    precision = data["precision"]  # type: ignore[assignment]
    recall = data["recall"]  # type: ignore[assignment]
    synthetic = data["synthetic"]  # type: ignore[assignment]
    scale = data["scale"]  # type: ignore[assignment]
    ground_truth = data["ground_truth"]  # type: ignore[assignment]

    lines: list[str] = [
        "# Generated metrics",
        "",
        "<!-- Generated by `make metrics`. Do not edit by hand. -->",
        "",
        "Pinned inputs: "
        + ", ".join(f"{name} `{sha[:7]}`" for name, (_, sha) in PINNED.items())
        + ". Intervals are 95% Wilson score intervals.",
        f"Test suite (generated): {data['tests']['count']} tests collected.",
        "",
        "## Definitions",
        "",
        "- **Confirmed by Jedi (strict)** — Jedi's definition lands inside the claimed target's",
        "  file and line span.",
        "- **Not contradicted by Jedi (canonical)** — strict, or Jedi lands on a file that",
        "  re-exports the claimed symbol. This is not precision and is not the headline.",
        "- **Hand-labeled precision (headline)** — human verdict on the blind 100-edge packet.",
        "  requests and click are labeled; zod is still unlabeled.",
        "- **Conditional recall** — found edges divided by call sites Jedi resolves to a project",
        "  symbol. **Unconditional recall is UNMEASURED.**",
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
        "## Python call-site ground truth (Jedi, `src/` only)",
        "",
        "Pinned: "
        + ", ".join(
            f"{name} `{sha[:7]}`" for name, sha in ground_truth["pinned"].items()
        )
        + ". Every raw tree-sitter call site is resolved with Jedi (`follow_imports=True`).",
        "Precision = correct/(correct+wrong); recall = correct/project sites. High-confidence",
        "means the tool's reported edge confidence is at least 0.95.",
        "",
        "| Repo | Sites | Correct | Wrong | Missed | Ambiguous (reported) | Precision | "
        "High-conf precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for name, entry in ground_truth["repos"].items():
        high = entry["high_confidence"]
        p_low, p_high = entry["precision_ci95"]
        r_low, r_high = entry["recall_ci95"]
        lines.append(
            f"| `{name}` | {entry['project_sites']} | {entry['correct']} | "
            f"{entry['wrong']} | {entry['missed']} | "
            f"{entry['ambiguous']} ({entry['ambiguous_but_reported']}) | "
            f"{fmt(entry['precision'])} (95% CI {fmt(p_low)}–{fmt(p_high)}) | "
            f"{fmt(high['precision'])} ({high['correct']}/{high['claims']}) | "
            f"{fmt(entry['recall'])} (95% CI {fmt(r_low)}–{fmt(r_high)}) |"
        )
    combined = ground_truth["combined"]
    high = combined["high_confidence"]
    lines += [
        "",
        f"Combined: {combined['correct']} correct, {combined['wrong']} wrong, "
        f"{combined['missed']} missed of {combined['project_sites']} project sites; "
        f"precision {fmt(combined['precision'])}, recall {fmt(combined['recall'])}, "
        f"high-confidence precision {fmt(high['precision'])} "
        f"({high['correct']}/{high['claims']}).",
        "",
        "| Form | Sites | Correct | Wrong | Missed | Ambiguous reported | Precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for form, row in combined["by_form"].items():
        if not row["sites"]:
            continue
        precision = row["correct"] / (row["correct"] + row["wrong"]) if (
            row["correct"] + row["wrong"]
        ) else 0.0
        lines.append(
            f"| `{form}` | {row['sites']} | {row['correct']} | {row['wrong']} | "
            f"{row['missed']} | {row['ambiguous_but_reported']} | {fmt(precision)} | "
            f"{fmt(row['correct'] / row['sites'])} |"
        )
    audit = ground_truth.get("hand_audit") or {}
    lines += [
        "",
        f"Independent hand audit: {audit.get('correct', 0)}/{audit.get('sample', 0)} sampled "
        "unjudged edges correct, with file:line citations in "
        "`metrics/python_ground_truth.json`.",
        "",
    ]
    ts_gt = data["ts_ground_truth"]  # type: ignore[assignment]
    if not ts_gt.get("repos"):
        lines += ["## TypeScript/JavaScript call-site ground truth", "", "UNMEASURED", ""]
    else:
        scope_bits = []
        for name, scope in ts_gt["scope"].items():
            bit = f"{name} `{ts_gt['pinned'][name][:7]}` ({scope['root']}"
            if scope.get("exclude"):
                bit += f"; excludes {scope['exclude']}"
            scope_bits.append(bit + ")")
        lines += [
            "## TypeScript/JavaScript call-site ground truth (TypeScript compiler)",
            "",
            "Pinned: " + ", ".join(scope_bits) + ".",
            "Every call, `new`, and JSX tag in production source is resolved with "
            "`checker.getSymbolAtLocation` (aliases followed); test and story files are "
            "excluded per repo.",
            "Precision = correct/(correct+wrong); recall = correct/project sites. "
            "High-confidence means the tool's reported confidence is at least 0.95.",
            "",
            "| Repo | Group | Sites | Correct | Wrong | Missed | Ambiguous (reported) | "
            "Precision | High-conf precision | Recall |",
            "|---|---|---:|---:|---:|---:|---:|---|---|---|",
        ]
        for name, entry in ts_gt["repos"].items():
            for group, label in (("ts", "TS+TSX"), ("js", "JS+JSX")):
                row = entry["groups"][group]
                if not row["project_sites"]:
                    continue
                high = row["high_confidence"]
                p_low, p_high = row["precision_ci95"]
                r_low, r_high = row["recall_ci95"]
                lines.append(
                    f"| `{name}` | {label} | {row['project_sites']} | {row['correct']} | "
                    f"{row['wrong']} | {row['missed']} | {row['ambiguous']} "
                    f"({row['ambiguous_but_reported']}) | "
                    f"{fmt(row['precision'])} (95% CI {fmt(p_low)}–{fmt(p_high)}) | "
                    f"{fmt(high['precision'])} ({high['correct']}/{high['claims']}) | "
                    f"{fmt(row['recall'])} (95% CI {fmt(r_low)}–{fmt(r_high)}) |"
                )
        lines += [
            "",
            "| Group | Sites | Correct | Wrong | Missed | Ambiguous (reported) | "
            "Precision | High-conf precision | Recall |",
            "|---|---:|---:|---:|---:|---:|---|---|---|",
        ]
        for group, label in (("ts", "TS+TSX"), ("js", "JS+JSX")):
            row = ts_gt["groups"].get(group)
            if not row:
                continue
            high = row["high_confidence"]
            p_low, p_high = row["precision_ci95"]
            r_low, r_high = row["recall_ci95"]
            lines.append(
                f"| **{label}** | {row['project_sites']} | {row['correct']} | "
                f"{row['wrong']} | {row['missed']} | {row['ambiguous']} "
                f"({row['ambiguous_but_reported']}) | "
                f"{fmt(row['precision'])} (95% CI {fmt(p_low)}–{fmt(p_high)}) | "
                f"{fmt(high['precision'])} ({high['correct']}/{high['claims']}) | "
                f"{fmt(row['recall'])} (95% CI {fmt(r_low)}–{fmt(r_high)}) |"
            )
        lines += [
            "",
            "| Group | Form | Sites | Correct | Wrong | Missed | Ambiguous reported | "
            "Precision | Recall |",
            "|---|---|---:|---:|---:|---:|---:|---|---|",
        ]
        for group, label in (("ts", "TS+TSX"), ("js", "JS+JSX")):
            row = ts_gt["groups"].get(group)
            if not row:
                continue
            for form, frow in row["by_form"].items():
                lines.append(
                    f"| {label} | `{form}` | {frow['sites']} | {frow['correct']} | "
                    f"{frow['wrong']} | {frow['missed']} | "
                    f"{frow['ambiguous_but_reported']} | {fmt(frow['precision'])} | "
                    f"{fmt(frow['recall'])} |"
                )
        gate_bits = []
        for group, label in (("ts", "TS+TSX"), ("js", "JS+JSX")):
            row = ts_gt["groups"].get(group)
            if not row:
                continue
            passed = (
                row["high_confidence"]["precision"] >= 0.95
                and row["precision"] >= 0.90
                and row["recall"] >= 0.50
            )
            gate_bits.append(f"{label} {'PASS' if passed else 'FAIL'}")
        lines += [
            "",
            "Gate (high-confidence precision >= 95%, overall precision >= 90%, "
            "recall >= 50%): " + "; ".join(gate_bits) + ".",
        ]
        audit = ts_gt.get("hand_audit") or {}
        lines += [
            "",
            f"Independent hand audit: {audit.get('correct', 0)}/{audit.get('sample', 0)} "
            "sampled edges the compiler cannot judge were correct, with file:line citations "
            "in `metrics/ts_ground_truth.json`.",
            "",
        ]
    sizes = data["size_comparison"]  # type: ignore[assignment]
    if sizes.get("per_language"):
        lines += [
            "## Slice size comparison (30 random symbols per language, seed 42)",
            "",
            "Default slice payload tokens vs the entry symbol's whole file vs every unique "
            "file containing its callers and callees. Sizes only; no sufficiency claim.",
            "",
            "| Language | Symbols | Payload tokens p10 / median / p90 | "
            "Entry file p10 / median / p90 | Caller+callee files p10 / median / p90 |",
            "|---|---:|---|---|---|",
        ]
        for language, entry in sizes["per_language"].items():
            rows = []
            for key in ("payload_tokens", "entry_file_tokens", "caller_callee_files_tokens"):
                q = entry[key]
                rows.append(f"{q['p10']:.0f} / {q['median']:.0f} / {q['p90']:.0f}")
            lines.append(
                f"| {language} | {entry['symbols']} | {rows[0]} | {rows[1]} | {rows[2]} |"
            )
        lines += [""]
    lines += [
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
    (METRICS_DIR / "python_ground_truth.json").write_text(
        json.dumps(data["ground_truth"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "ts_ground_truth.json").write_text(
        json.dumps(data["ts_ground_truth"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "size_comparison.json").write_text(
        json.dumps(data["size_comparison"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "heldout_ground_truth.json").write_text(
        json.dumps(data["heldout"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "examples.json").write_text(
        json.dumps(data["examples"], indent=2) + "\n", encoding="utf-8"
    )
    (METRICS_DIR / "tests.json").write_text(
        json.dumps(data["tests"], indent=2) + "\n", encoding="utf-8"
    )



def render_readme(data: dict[str, object]) -> str:
    python = data["ground_truth"]  # type: ignore[assignment]
    ts = data["ts_ground_truth"]  # type: ignore[assignment]
    heldout = data["heldout"]  # type: ignore[assignment]
    sizes = data["size_comparison"]  # type: ignore[assignment]
    performance = data["performance"]  # type: ignore[assignment]

    records: list[dict[str, object]] = []
    for name, entry in python["repos"].items():
        records.append(
            {
                "name": name,
                "language": "Python",
                "sha": entry.get("sha", python["pinned"].get(name, "")),
                "entry": entry,
                "source": "main",
            }
        )
    for name, entry in heldout["results"].items():
        if entry["language"] != "python":
            continue
        records.append(
            {
                "name": name,
                "language": "Python",
                "sha": entry["sha"],
                "entry": entry,
                "source": "held-out",
            }
        )
    for name, entry in ts["repos"].items():
        group = "ts" if entry["groups"]["ts"]["project_sites"] else "js"
        label = "TypeScript" if group == "ts" else "JavaScript"
        records.append(
            {
                "name": name,
                "language": label,
                "sha": entry.get("sha", ""),
                "entry": entry["groups"][group],
                "source": "main",
            }
        )
    for name, entry in heldout["results"].items():
        if entry["language"] == "python":
            continue
        label = "TypeScript" if entry["language"] == "typescript" else "JavaScript"
        records.append(
            {
                "name": name,
                "language": label,
                "sha": entry["sha"],
                "entry": entry,
                "source": "held-out",
            }
        )

    result_lines = [
        "| Repo | Language | Pinned SHA | Sites | Precision (95% CI) | High-conf | "
        "Recall (95% CI) | Source |",
        "|---|---|---|---:|---|---:|---|---|",
    ]
    aggregate: dict[str, dict[str, int]] = {}
    for record in records:
        entry = record["entry"]  # type: ignore[assignment]
        sites = int(entry.get("project_sites", entry.get("sites", 0)))
        correct = int(entry["correct"])
        wrong = int(entry["wrong"])
        high = entry["high_confidence"]
        p_low, p_high = entry["precision_ci95"]
        r_low, r_high = entry["recall_ci95"]
        result_lines.append(
            f"| `{record['name']}` | {record['language']} | "
            f"`{str(record['sha'])[:7]}` | {sites} | "
            f"{fmt(entry['precision'])} (95% CI {fmt(p_low)}–{fmt(p_high)}) | "
            f"{high['correct']}/{high['claims']} | "
            f"{fmt(entry['recall'])} (95% CI {fmt(r_low)}–{fmt(r_high)}) | "
            f"{record['source']} |"
        )
        bucket = aggregate.setdefault(
            str(record["language"]),
            {"sites": 0, "correct": 0, "wrong": 0, "hc_claims": 0, "hc_correct": 0},
        )
        bucket["sites"] += sites
        bucket["correct"] += correct
        bucket["wrong"] += wrong
        bucket["hc_claims"] += int(high["claims"])
        bucket["hc_correct"] += int(high["correct"])
    for language, bucket in aggregate.items():
        total = bucket["correct"] + bucket["wrong"]
        low, high = wilson(bucket["correct"], total)
        r_low, r_high = wilson(bucket["correct"], bucket["sites"])
        precision = bucket["correct"] / total if total else 0.0
        high_precision = (
            bucket["hc_correct"] / bucket["hc_claims"] if bucket["hc_claims"] else 0.0
        )
        recall = bucket["correct"] / bucket["sites"] if bucket["sites"] else 0.0
        result_lines.append(
            f"| **{language} (all)** | | | {bucket['sites']} | {fmt(precision)} "
            f"(95% CI {fmt(low)}–{fmt(high)}) | "
            f"{bucket['hc_correct']}/{bucket['hc_claims']} ({fmt(high_precision)}) | "
            f"{fmt(recall)} (95% CI {fmt(r_low)}–{fmt(r_high)}) | |"
        )

    performance_lines = [
        "| Corpus | Files | Cold ms | Warm ms | Refresh median ms | Refresh p95 ms | "
        "Slowest refresh file | Query ms | Peak RSS MB |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for name, row in performance["results"].items():
        slowest = row["refresh_slowest"][0]
        performance_lines.append(
            f"| {name} | {row['files']} | {row['cold_ms']} | {row['warm_ms']} | "
            f"{row['refresh_median_ms']} | {row['refresh_p95_ms']} | "
            f"`{slowest['file']}` ({slowest['ms']} ms) | {row['query_ms']} | "
            f"{row['peak_rss_mb']} |"
        )

    size_lines = [
        "| Language | Symbols | Payload tokens p10 / median / p90 | "
        "Entry file p10 / median / p90 | Caller+callee files p10 / median / p90 |",
        "|---|---:|---|---|---|",
    ]
    for language, entry in sizes["per_language"].items():
        cells = []
        for key in ("payload_tokens", "entry_file_tokens", "caller_callee_files_tokens"):
            quantile = entry[key]
            cells.append(
                f"{quantile['p10']:.0f} / {quantile['median']:.0f} / {quantile['p90']:.0f}"
            )
        size_lines.append(
            f"| {language} | {entry['symbols']} | {cells[0]} | {cells[1]} | {cells[2]} |"
        )

    language_lines = [
        "| Extension | Language | Tree-sitter grammar |",
        "|---|---|---|",
    ]
    for extension, language in sorted(EXTENSION_TO_LANGUAGE.items()):
        language_lines.append(f"| `{extension}` | {language} | `{language}` |")

    examples = data["examples"]  # type: ignore[assignment]
    replacements = {
        "python_example": examples["python"]["compiled_context"].strip(),
        "typescript_example": examples["typescript"]["compiled_context"].strip(),
        "results_table": "\n".join(result_lines),
        "performance_table": "\n".join(performance_lines),
        "size_table": "\n".join(size_lines),
        "language_table": "\n".join(language_lines),
        "test_count": str(data["tests"]["count"]),  # type: ignore[index]
    }
    template = (ROOT / "README.template.md").read_text(encoding="utf-8")
    for key, value in replacements.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline: verify METRICS.md")
    parser.add_argument("--render", action="store_true", help="offline: rewrite METRICS.md")
    parser.add_argument("--repos-dir", type=Path, default=Path("/tmp/eval-repos"))
    args = parser.parse_args()

    if args.render:
        data = load_artifacts()
        (ROOT / "METRICS.md").write_text(render(data), encoding="utf-8")
        (ROOT / "README.md").write_text(render_readme(data), encoding="utf-8")
        print("rewrote METRICS.md and README.md from artifacts and labels")
        return

    if args.check:
        data = load_artifacts()
        for name, rendered in (
            ("METRICS.md", render(data)),
            ("README.md", render_readme(data)),
        ):
            current = (ROOT / name).read_text(encoding="utf-8")
            if current != rendered:
                diff = difflib.unified_diff(
                    current.splitlines(), rendered.splitlines(), fromfile=name, lineterm=""
                )
                print("\n".join(list(diff)[:200]))
                raise SystemExit(f"{name} does not match committed artifacts")
        print("METRICS.md and README.md match committed artifacts")
        return

    data = collect(args.repos_dir)
    write_artifacts(data)
    (ROOT / "METRICS.md").write_text(render(data), encoding="utf-8")
    (ROOT / "README.md").write_text(render_readme(data), encoding="utf-8")
    print("wrote README.md, METRICS.md and metrics/*.json")


if __name__ == "__main__":
    main()
