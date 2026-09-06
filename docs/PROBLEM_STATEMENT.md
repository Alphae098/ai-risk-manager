# Problem Statement — AI Risk Manager

**Author:** Alpha Ekhristi
**Date:** 2026-09-05
**Context:** Portfolio project for the Razorpay AI Builder Internship 2026

---

## 1. Background

A payment aggregator sits between millions of customers and hundreds of thousands of
merchants. Every authorization request it forwards carries two opposing costs. Approving a
fraudulent payment produces a chargeback: the aggregator refunds the cardholder, absorbs a
network penalty, and — if the merchant has already been settled and has no balance — eats the
principal. Declining a legitimate payment produces a false decline: lost revenue for the
merchant, a support ticket, and a customer who may not return.

Card networks compound the pressure at the merchant level. A merchant whose chargeback ratio
crosses roughly 0.9% of monthly volume enters a network monitoring program that carries
per-transaction fines and, if unresolved, delisting. The aggregator inherits that liability,
so risk must be managed on two clocks at once: milliseconds for the individual transaction,
and days-to-weeks for the merchant portfolio.

## 2. The problem

Existing defenses fail in three distinct ways, and each failure has a different cause.

**Rules are fast but brittle.** Hand-written rules ("decline if more than 5 attempts from one
card in 10 minutes") are auditable and cheap, and they catch known attacks. But every new
fraud pattern requires a human to notice it, write a rule, and deploy it — a loop measured in
days while losses accumulate. Rules also accrete: a mature engine holds thousands of them,
nobody knows which still fire, and their interactions produce false declines nobody can trace.

**Machine learning is accurate but silent.** A gradient-boosted model on velocity and behavioral
features materially outperforms rules on ranking. It cannot, however, explain a decision to a
merchant, a regulator, or the analyst who has to act on it. Worse, models are confident only at
the extremes. In the middle of the score distribution the model is genuinely uncertain, and
that uncertain band is where the expensive mistakes live.

**Human review does not scale.** The standard answer to the uncertain band is a manual review
queue. Human analysts are accurate and explainable, but they are slow, expensive, and finite.
Queue depth grows with volume; review quality degrades under backlog pressure; and the
reasoning an analyst applies is never captured in a form the system can reuse.

Underneath all three sits a fourth failure: **the merchant dimension is handled separately or not
at all.** Transaction scoring treats each payment as independent, while the merchant's own risk
posture — its chargeback trend, its settlement exposure, whether its traffic mix changed last
week — is reviewed on a different system by a different team, if at all. A transaction that is
unremarkable in isolation is often obviously suspicious once the merchant's recent trajectory is
visible, and that context never reaches the decision.

## 3. Problem statement

> Build a risk system that decides payment authorizations in real time with the accuracy of a
> machine-learned model, the explainability of a human analyst, and a per-transaction cost that
> stays flat as volume grows — while simultaneously maintaining a merchant-level risk view that
> feeds back into those transaction decisions.

## 4. Scope

**In scope**

- Real-time scoring of a synthetic Razorpay-shaped payment stream (UPI, card, netbanking, wallet).
- A deterministic rules layer for hard blocks and regulatory constraints.
- A supervised model producing a calibrated fraud probability.
- An LLM risk-analyst agent that investigates only transactions in the uncertain band, using
  tools to gather evidence, and returns a cited verdict.
- A merchant portfolio layer: rolling risk score, chargeback trend, and an agent-written
  underwriting memo recommending rolling reserve and transaction limits.
- An analyst dashboard: live stream, case queue, evidence view, override controls, metrics.
- An evaluation harness reporting precision, recall, review rate, LLM cost per 1,000
  transactions, and net financial impact against baselines.

**Out of scope**

- Real payment data, real PII, or any live payment network integration.
- Production authentication, multi-tenancy, and horizontal scaling.
- Regulatory certification (PCI-DSS, RBI compliance) — the design will note where these
  constraints would bind, but the project will not implement them.
- Model training infrastructure beyond a single reproducible training script.

## 5. Success criteria

The system is successful if, measured on a held-out synthetic period against a rules-only
baseline and a model-only baseline:

1. **Detection.** Recall on fraudulent transactions is at least 15 percentage points above the
   rules-only baseline at an equal or lower false-positive rate.
2. **Cost control.** The agent is invoked on no more than 10% of transactions, and LLM cost per
   1,000 transactions is reported explicitly and stays flat as volume grows.
3. **Explainability.** Every declined or escalated transaction carries a human-readable
   rationale naming the specific evidence that drove it. No decision is unexplained.
4. **Latency.** The non-agent path (rules plus model) decides in under 50ms at p95. The agent
   path is asynchronous and does not block authorization.
5. **Merchant linkage.** At least one demonstrable case where merchant-level context changes the
   verdict on a transaction that transaction-level features alone would have passed.
6. **Feedback.** Analyst overrides and arriving chargebacks are captured as labels and visibly
   change subsequent behavior.

## 6. Constraints and assumptions

- Data is synthetic and generated by this project. Fraud labels are known by construction,
  which makes evaluation exact but risks overstating real-world performance; the generator is
  therefore built to produce overlapping, ambiguous cases rather than cleanly separable ones.
- The LLM is reached through OpenRouter behind a thin provider-agnostic interface, with a
  deterministic mock so the full demo runs offline and tests never call a network.
- Single-node deployment. Data lives in SQLite with a schema that ports to Postgres unchanged.
- The system decides; it never moves money. All financial effects are simulated.

## 7. Why this design and not the obvious alternatives

Sending every transaction to an LLM is the simplest agentic design and the wrong one: it is
slow, costs scale linearly with volume, and its output is non-deterministic in a domain that
requires reproducible decisions. Using only a model is cheap and fast but produces no rationale
and no path to handling novel patterns. Using only rules cannot keep up with adversaries.

The design here routes by confidence. Deterministic layers handle the ~90% of traffic where the
answer is clear, and the expensive reasoning layer is spent only where uncertainty is real —
which is exactly how a well-run human risk team allocates its analysts.
