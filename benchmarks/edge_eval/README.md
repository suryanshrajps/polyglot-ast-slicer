# Edge precision and recall harness

Independent reference: **Jedi 0.20.1** (`pip install jedi`). Pinned repositories:

| Repo | Commit |
|---|---|
| `pallets/click` | `2247b35ea1c47c727d7a06e51fa280e12a863ff6` |
| `psf/requests` | `3c7baaa05ab4aa49cc819ce3a819d260de19c157` |

```bash
python benchmarks/edge_eval/precision.py /path/to/click 100   # 100 random edges, seed 42
python benchmarks/edge_eval/recall.py    /path/to/click 150   # 150 random call sites
```

`precision.py` resolves each edge's call site with Jedi and counts an edge as supported when
Jedi lands on the callee, either directly (`strict`) or through a re-export the engine also
resolves (`canonical`). `recall.py` does the inverse: every sampled call site Jedi can resolve
to a project symbol is checked against the graph.

Caveats, stated up front:

- Jedi is a reference implementation, not ground truth. It cannot resolve untyped fixtures or
  dynamic receivers, so recall is conditional on Jedi resolving the site.
- Canonical matching consults the engine's import table for the final re-export hop; that is a
  deliberate relaxation to avoid counting `pkg/__init__.py` re-exports as misses.
- Samples are seeded (`42` for precision, `7` for recall) and reproducible. The labelled edge
  files are not committed; rerun the scripts to regenerate the numbers.
