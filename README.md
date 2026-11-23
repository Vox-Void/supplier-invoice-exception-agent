# Supplier Invoice Exception Agent

Check an invoice against its purchase order and posted receipts. Save an evidence-backed draft in a review queue so accounts payable can decide what to do next.

The demo uses a fictional South African retailer. The PO covers 100 units. The warehouse received 80 before the cutoff, and an earlier invoice used 30. The new invoice asks for 60 of the 50 available, so the draft records a ten-unit shortfall and proposes requesting the receipt.

## Run it

Use Bash on Linux or WSL2 with Docker Compose v2 and uv 0.13.9. The project pins Python 3.11.15 and PostgreSQL 17.11. The first run downloads the dependencies and database image.

```bash
./scripts/dev bootstrap
./scripts/dev test
./scripts/dev demo
./scripts/dev eval
```

The demo exercises the FastAPI routes in-process against PostgreSQL. It recreates the app midway and reads the same case back before recording a review decision. The default demo and evaluation use fixture recordings and require no API key.

For a running server, start `./scripts/dev serve` and open [the local API explorer](http://127.0.0.1:8014/docs). The [runbook](docs/runbook.md) has commands for the request, the queue and the review decision. The [project guide](docs/project-guide.md) follows the evidence from the purchase order through to the saved case.

## What to inspect

- `artifacts/demo.json` shows the saved draft, the repeated request and the audit events.
- `artifacts/evaluation-recorded.json` compares a fixed tool order and recorded proposals on the same fixtures. Cases pass only when the saved outcome and evidence match the expected result.
- `fixtures/` holds the synthetic snapshots, expected results and tool call recordings.
- `db/schema.sql` defines storage for fixture records, investigation leases and reviewer decisions.

This is a local review workflow for structured invoices in ZAR. It leaves supplier contact, accounting entries and payments with the reviewer.
