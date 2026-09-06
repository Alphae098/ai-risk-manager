# Evaluation report

Held-out period: 18223 transactions, 429 fraudulent (2.35%).

## Strategy comparison

| Strategy | Declined | Reviewed | Precision | Recall | FP rate | Net INR |
|---|---|---|---|---|---|---|
| rules only | 34 | 0 | 1.000 | 0.079 | 0.0000 | 115,153 |
| model only | 323 | 0 | 0.985 | 0.741 | 0.0003 | 3,644,976 |
| hybrid (rules + model + agent) | 323 | 253 | 0.989 | 0.823 | 0.0002 | 3,730,918 |

## Agent

- Cases resolved: 300
- Catch rate on review-band fraud: 41.9%
- Precision when it calls fraud: 84.4%
- Escalated to a human: 18.0%
- Cost per case: $0.0000  (offline heuristic reviewer - no LLM calls were made)

## Latency (features + rules + model, single-transaction path)

- samples: 800
- p50_ms: 11.233
- p95_ms: 13.104
- p99_ms: 14.644
- max_ms: 62.008
- mean_ms: 11.094

## Band sweep

| Band | Review rate | Recall | Precision | LLM $/1k txn | Net INR |
|---|---|---|---|---|---|
| 0.50-0.90 | 0.54% | 0.760 | 0.994 | 0.0000 | 3,469,664 |
| 0.40-0.90 | 0.58% | 0.765 | 0.994 | 0.0000 | 3,491,807 |
| 0.30-0.85 | 0.63% | 0.781 | 0.991 | 0.0000 | 3,573,785 |
| 0.20-0.85 | 0.85% | 0.790 | 0.991 | 0.0000 | 3,596,444 |
| 0.15-0.80 | 1.39% | 0.823 | 0.989 | 0.0000 | 3,730,918 |
| 0.10-0.75 | 1.51% | 0.846 | 0.986 | 0.0000 | 3,848,663 |
| 0.05-0.70 | 1.57% | 0.860 | 0.987 | 0.0000 | 3,898,021 |
