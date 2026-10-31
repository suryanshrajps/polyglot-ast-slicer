# Guidelines for AI agents on polyglot-ast-slicer
Bias: caution over speed. For trivial tasks, use judgment.
1. Think before coding: state assumptions; ask only if blocked, otherwise state the assumption and proceed; list interpretations instead of picking silently; say so if a simpler approach exists.
2. Simplicity first: minimum code for the request; no speculative features, single-use abstractions or unrequested configurability; no error handling for impossible cases.
3. Surgical changes: every changed line traces to the request; do not restyle or refactor adjacent code; mention unrelated dead code instead of deleting it; remove only what your change orphaned.
4. Goal-driven: bug -> failing test first, then fix; refactor -> tests green before and after; for multi-step work write a plan "step -> verify: check".
Project rules: never invent numbers, APIs or results (unmeasured = UNMEASURED); every figure in README/METRICS comes from a committed script; prefer a missing edge to a wrong edge; never relabel data or lower a gate to pass; engine changes need a regression test plus a re-run of the labeled precision samples.
Token discipline: don't re-read known files; don't print full test logs; keep reports short.
