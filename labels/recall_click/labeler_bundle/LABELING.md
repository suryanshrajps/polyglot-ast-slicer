# Blind recall labeling

`packets.json` lists call sites with the exact call expression marked by `^^^^`.
No tool output is included.

For each site:
1. Read the call expression and surrounding code.
2. Find the definition this call actually refers to by reading the repository
   (imports, class hierarchy, aliases). Write `<path>:<line>` in `true_target`.
3. If you cannot determine it, leave `true_target` empty and say why in `notes`.

Do not use this project's tooling or any static analyzer; label by reading.
