# Evaluation report

Held-out period: 18463 transactions, 678 fraudulent (3.67%).

## Strategy comparison

| Strategy | Declined | Reviewed | Precision | Recall | FP rate | Net INR |
|---|---|---|---|---|---|---|
| rules only | 96 | 0 | 1.000 | 0.142 | 0.0000 | 208,811 |
| model only | 497 | 0 | 0.964 | 0.707 | 0.0010 | 4,662,790 |
| hybrid (rules + model + agent) | 497 | 129 | 0.996 | 0.802 | 0.0001 | 4,555,400 |

## Agent

- Cases resolved: 300
- Catch rate on review-band fraud: 49.0%
- Precision when it calls fraud: 88.6%
- Escalated to a human: 16.7%
- Cost per case: $0.0000  (offline heuristic reviewer - no LLM calls were made)

## Latency (features + rules + model, single-transaction path)

- samples: 800
- p50_ms: 6.166
- p95_ms: 9.889
- p99_ms: 11.441
- max_ms: 21.868
- mean_ms: 6.745

## Band sweep

| Band | Review rate | Recall | Precision | LLM $/1k txn | Net INR |
|---|---|---|---|---|---|
| 0.50-0.90 | 0.38% | 0.765 | 0.996 | 0.0000 | 4,317,178 |
| 0.40-0.90 | 0.50% | 0.776 | 0.996 | 0.0000 | 4,386,006 |
| 0.30-0.85 | 0.70% | 0.802 | 0.996 | 0.0000 | 4,555,400 |
| 0.20-0.85 | 0.99% | 0.820 | 0.996 | 0.0000 | 4,644,910 |
| 0.15-0.80 | 1.65% | 0.835 | 0.997 | 0.0000 | 4,730,481 |
| 0.10-0.75 | 1.83% | 0.842 | 0.997 | 0.0000 | 4,765,895 |
| 0.05-0.70 | 2.10% | 0.853 | 0.995 | 0.0000 | 4,811,546 |
