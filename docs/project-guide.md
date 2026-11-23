# Project guide

## The work this replaces

When an invoice differs from a purchase order or a delivery record, an accounts-payable analyst has to compare all three sources. Retrieving the records is often the slow part. The application collects them into one case, explains the calculations and saves a proposed next action for review.

The project uses a fictional retailer and a fixed snapshot. Every receipt and prior allocation has both an event time and a recorded time. A record counts only if both timestamps are on or before the cutoff. That lets a reviewer repeat an investigation later and see the same facts.

## Follow the headline invoice

1. A requester submits invoice `INV-060` with snapshot `retail-demo-v1` and an idempotency key. PostgreSQL assigns one worker a lease for that key.
2. The workflow retrieves the invoice, purchase order, receipts and previous allocations. Each tool response carries the record IDs, revisions and a digest.
3. The matching policy checks which records support the calculation. For this invoice, 100 ordered less 30 already allocated leaves 70 available under the PO. Of the 80 received, 50 remain. The smaller figure is 50. The 60-unit request is ten units short.
4. The application validates the proposed finding and action against the evidence from the tools. It writes the draft and a queue event in the same database transaction.
5. A reviewer opens the case, sees the records behind the shortfall and records a disposition. If two reviewers act on the same version, only the first change can commit.

## How it was built

The matching rules came first. The fixtures cover partial and split receipts, reversals, late records, duplicates and price boundaries. Each expected result is kept with the fixtures so someone can review the math without working through the Python.

The application then added five read tools with a fixed tenant, snapshot and invoice context. The fixed-order baseline and agent both use these tools. The recorded adapter replays declared tool selections and verifies each returned digest. The live adapter uses the same scope and validation for an explicitly enabled OpenAI run.

The policy checks quantities as integers in eaches and prices as exact decimals. It accepts a difference of one cent per unit and flags differences above that. Line net values use half-up rounding. If the evidence is incomplete, the case describes the gap and preserves any findings the app can still verify.

The last part was persistence. The requester reserves a key in a short transaction, and tool calls run after that transaction ends. The app checks the lease again before it writes. If the HTTP response is lost after the commit, the repeated request finds the original case. After an expired lease, a new worker can claim the same run. Only the current worker can finish it.

## Assumptions and next steps

- The first release handles structured fixtures. Real invoices would need ingestion, supplier identity mapping and a way to resolve OCR mistakes.
- The local API uses two demo roles and a loopback address. A deployment would need real identity and access management, more granular permissions and an audit retention policy.
- The database guarantees one case per completed run. A worker can still repeat model calls after losing a lease or encountering a failure. A deployment should measure that cost and add retry limits.
- The demo records a review decision inside the application. A later ERP integration would need a separately authorised workflow and a reconciliation check.
- The benchmark measures fixture behavior. Add more held-out receipt and allocation patterns before judging a model on broader invoice traffic.

See the [runbook](runbook.md) for test commands and operating details.
