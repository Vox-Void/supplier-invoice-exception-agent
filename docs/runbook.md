# Running the invoice agent

## Start from a clean clone

From the repository root, run `./scripts/dev bootstrap`. This starts PostgreSQL on loopback, installs the locked Python environment and inserts the fixture snapshots. Running it again checks the snapshot digest and keeps existing cases. Compose stores the database in a named volume.

Run `./scripts/dev test` for formatting, lint, types and tests. The PostgreSQL tests create and clean up temporary databases. If you supply your own DSN, use a local test role that can create databases. To move the Compose port, set `INVOICE_AGENT_PG_PORT` before bootstrap and use the same environment for the other commands.

## Work through one case

Run `./scripts/dev serve` in one terminal. In another, set these synthetic local credentials or use the values you configured for the server.

```bash
base=http://127.0.0.1:8014
requester="${DEMO_REQUESTER_TOKEN:-demo-requester-local}"
reviewer="${DEMO_REVIEWER_TOKEN:-demo-reviewer-local}"
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $requester" \
  -H "Idempotency-Key: receipt-check-01" \
  -H "Content-Type: application/json" \
  -d '{"invoice_id":"INV-060","snapshot_id":"retail-demo-v1","mode":"recorded"}' \
  "$base/v1/investigations"
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $reviewer" \
  "$base/v1/cases?status=READY_FOR_REVIEW"
```

The first request returns HTTP 201. Reuse the same key and body to get HTTP 200 and the same case ID. HTTP 202 means the existing run still has an active lease. Poll `GET /v1/investigations/{run_id}`. HTTP 503 carries a run ID when the attempt can be retried. Reuse the key for that retry. A fresh key starts a separate investigation. The reviewer decision accepts an expected case version and its own idempotency key. Use the [local API explorer](http://127.0.0.1:8014/docs) for the full payload and error schemas.

## Select the live adapter

An enabled server still defaults to recorded mode. Set `INVOICE_AGENT_ENABLE_LIVE=true`, `INVOICE_AGENT_MODEL` and `OPENAI_API_KEY` in the process environment before startup and explicitly send `"mode":"live"` when starting an investigation. The CLI command `./scripts/dev eval --mode live` runs the synthetic headline only. Live mode can use provider credits. Local logs show structured tool digests and outcomes without the provider key or the raw model draft.

The live adapter limits turns, accepted tool calls, rows per result, time and token use. Only fictional case evidence is sent. Provider errors use a retryable response. A draft with unsupported claims is escalated by the application.

## Testing notes

- The same-key concurrency test makes four workers claim one run and checks that only one receives a lease.
- The worker recovery test expires a lease, verifies the old worker can no longer commit, then verifies that a repeat request returns the saved case.
- The rollback test raises an error during the case write, then confirms the lease and queue remain consistent.
- The recorded safety cases include missing or truncated evidence, a memo that tries to direct the workflow and a draft that attaches a real source ID to the wrong finding.
