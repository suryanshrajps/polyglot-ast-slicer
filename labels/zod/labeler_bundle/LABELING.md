# Blind labeling instructions

You are given `packets_v2.json`. Each packet shows one call expression and the target the
tool claims it invokes.

For every packet:
1. Read `call_text`, `call_range` and the marked `call_context`. The `^^^^` line marks the
   exact call node; do not guess from the whole line.
2. Decide whether the claimed `target` is the definition that this call actually invokes.
   Check owner/module, not just the short name. Watch for sibling classes, overrides,
   different methods of the same class, and same-named functions in other modules.
3. Write a verdict: `correct`, `incorrect` or `unsure`.
4. Write a one-line justification citing `file:line` for the call and the target.

Do not use the repository's tooling, this project's code, or any static analyzer. Do not
look for an answer key; none is included. Label independently.
