# Stage B results

Commits: 30, usable: 29

| Metric | p10 | p50 | p90 | min | max |
|---|---:|---:|---:|---:|---:|
| co_change | 0.0 | 0.0 | 1.0 | 0.0 | 1.0 |
| test_recall | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| payload | 481 | 2759 | 5544 | 215 | 5784 |
| touched | 2463 | 15087 | 37349 | 282 | 43705 |
| grep | 1228 | 5921 | 102862 | 799 | 970678 |
| map | 12292 | 25978 | 31278 | 12269 | 32063 |

Baselines: payload beats grep 22/29, map 29/29, touched 27/29; grep wins 7, map wins 0.

## Failures

- sb_flask d086db85: no_changed_function
