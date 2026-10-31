"""Selects pinned bug-fix commits touching 1-5 non-test Python files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPOS = {"requests": "/tmp/sb_requests", "click": "/tmp/sb_click", "flask": "/tmp/sb_flask"}
PER_REPO = 10


def git(repo: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    )
    return completed.stdout


def main() -> None:
    selected = []
    for name, path in REPOS.items():
        log = git(path, "log", "--no-merges", "-i", "--grep=fix", "--grep=bug",
                  "--format=%H%x09%s", "-n", "600")
        count = 0
        for line in log.splitlines():
            if count >= PER_REPO:
                break
            sha, subject = line.split("\t", 1)
            numstat = git(path, "show", "--numstat", "--format=", sha)
            files = [
                row.split("\t")[2]
                for row in numstat.splitlines()
                if row.strip() and row.split("\t")[2].endswith(".py")
            ]
            non_test = [f for f in files if "test" not in f.lower()]
            if not (1 <= len(non_test) <= 5):
                continue
            diff = git(path, "show", "--unified=0", "--format=", sha)
            if "def " not in diff and "class " not in diff:
                continue
            selected.append({"repo": name, "sha": sha, "subject": subject,
                             "non_test_files": non_test})
            count += 1
    Path("benchmarks/stage_b/commits.json").write_text(json.dumps(selected, indent=2) + "\n")
    print(f"selected {len(selected)}")
    for item in selected:
        print(item["repo"], item["sha"][:8], item["subject"][:70])


if __name__ == "__main__":
    main()
