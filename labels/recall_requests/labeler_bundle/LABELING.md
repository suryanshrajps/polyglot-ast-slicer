# Blind recall labeling

`packets.json` lists call sites and callback references with the exact expression marked by
`^^^^`. No tool output is included.

For each site, read the repository and write the definition it refers to as `<path>:<line>`
in `true_target`. If it cannot be determined, leave it blank and explain in `notes`.

Do not use this project's tooling or any static analyzer.
