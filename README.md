# AI Risk Manager

A payment risk system that decides authorizations in real time with the accuracy of a
machine-learned model, the explainability of a human analyst, and an LLM cost that stays flat
as volume grows — while maintaining a merchant-level risk view that feeds back into those
transaction decisions.

Built as a portfolio project for the Razorpay AI Builder Internship 2026. The full problem
statement, including scope and success criteria, is in
[docs/PROBLEM_STATEMENT.md](docs/PROBLEM_STATEMENT.md).

## The idea in one paragraph

Sending every transaction to a language model is slow, expensive and non-deterministic.
Sending none of them gives you a model that cannot explain itself and cannot handle a pattern
it has never seen. So the system routes by confidence: deterministic rules and a calibrated
gradient-boosted model decide the ~99% of traffic where the answer is clear, and an LLM
analyst agent investigates only the uncertain band — asynchronously, after the payment has
been provisionally approved, so nothing blocks on an LLM. Because the agent runs after the
fact, it can see evidence the real-time scorer could not: the rest of a fraud ring that
transacted minutes later, and the merchant's risk trajectory over the past month.

## Decision path

```
transaction
    |
    v
[1] feature engine        velocity counters, entity graph, baseline deviation
    |
    v
[2] rules engine          YAML rules; a hard hit declines immediately, no model, no LLM
    |
    v
[3] fraud model           gradient boosting + isotonic calibration -> probability
    |
    v
[4] band router           < 0.15 approve | 0.15-0.80 review | >= 0.80 decline
    |                                        |
    |                                        v
    |                            [5] analyst agent (async)
    |                                tool-calling loop, bounded budget,
    |                                structured verdict with cited evidence
    v                                        |
 decision  <---------------------------------+
    |
    v
[6] outcomes              chargebacks, refunds, analyst overrides -> labels
                          + nightly merchant portfolio rollup
```

The scoring layer never imports the agent. That dependency direction is what keeps the
authorization path deterministic and testable without a language model in the loop.

## What is in the repository

```
backend/
  arm/
    simulator/    synthetic Razorpay-shaped payment stream, 6 injected fraud patterns
    features/     streaming feature engine, shared by training and serving
    scoring/      rules engine (YAML), calibrated model, band router
    agent/        OpenRouter client, investigation tools, the analyst agent
    merchant/     portfolio risk scoring, reserve and limit advice, underwriting memos
    backfill.py   replays the whole history through the pipeline
  eval/
    harness.py    strategy comparison, band sweep, cost and latency reporting
docs/
  PROBLEM_STATEMENT.md
```

## Results on the held-out period

18,223 transactions the model never saw during training, 429 of them fraudulent.

| Strategy | Precision | Recall | False-positive rate | Net (simulated) |
|---|---|---|---|---|
| Rules only | 1.000 | 0.079 | 0.0000 | INR 115,153 |
| Model only, same decline budget | 0.985 | 0.741 | 0.0003 | INR 3,644,976 |
| **Hybrid: rules + model + agent** | **0.989** | **0.823** | **0.0002** | **INR 3,730,918** |

Against the success criteria in the problem statement:

- **Detection.** +74 points of recall over rules alone, at a lower false-positive rate. The
  target was +15.
- **Cost control.** 1.4% of traffic reaches the agent, against a 10% ceiling. Cost per 1,000
  transactions is computed from recorded token usage, not estimated.
- **Latency.** The authorization path (features, rules, model) runs at 13.1 ms p95 against a
  50 ms target. The agent path is asynchronous and never blocks an authorization.
- **Explainability.** Every escalated case carries a verdict, a confidence, and evidence
  entries naming the tool that produced each fact.
- **Merchant linkage.** The bust-out merchants are the clearest case: their individual
  payments look ordinary, and the merchant risk trajectory is what exposes them.
- **Feedback.** Analyst overrides and arriving chargebacks are stored as outcomes and drive
  rule precision statistics and retraining.

Full report, including the band sweep: `data/evaluation.md`.

## Running it

```bash
pip install -r backend/requirements.txt
cd backend
python -m arm.simulator.generator   # generate the dataset
python -m arm.scoring.train         # train and calibrate the model
python -m arm.backfill              # score everything, roll up merchants, run agent cases
python -m eval.harness              # write the evaluation report
python -m pytest                    # 13 tests, no network required
```

Then the dashboard, in two terminals:

```bash
cd backend && python -m uvicorn arm.api.main:app --port 8000
```

```bash
cd frontend && npm install && npm run dev
```

The console opens at `http://localhost:5173`. Five screens: **Live** (the score spine, where
every payment docks at its own score and the shaded middle is the band that reaches the
agent), **Cases** (the queue, and the evidence behind each verdict), **Merchants** (portfolio
risk and underwriting memos), **Rules** (hit counts and measured precision per rule, so dead
rules are visible), and **Evidence** (the evaluation report).

Everything above runs offline. The agent falls back to a deterministic heuristic reviewer
when no API key is configured, so the demo and the tests never require a network call.

To use a real model, copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`. The model is
configurable via `ARM_LLM_MODEL`; any OpenRouter model that supports tool calling will work.

## Honest notes

- The data is synthetic and its labels are known by construction. That makes evaluation exact
  and it also means the headline detection numbers are optimistic relative to production. The
  generator deliberately includes overlapping cases — sale-day spikes, travelling customers,
  first-time high-ticket buyers, and a friendly-fraud pattern that is undetectable at
  authorization by design — so the numbers are not trivially perfect.
- The feature attribution shown to analysts reports deviation from the training median, not
  true model attribution. It is labelled as such in the UI and in the code.
- The system decides; it never moves money. All financial effects are simulated.

## Licence

MIT.
