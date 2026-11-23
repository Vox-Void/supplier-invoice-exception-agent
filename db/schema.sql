CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id text PRIMARY KEY,
    fixture_version text NOT NULL,
    policy_version text NOT NULL,
    as_of_utc timestamptz NOT NULL,
    sha256 text NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    snapshot_id text REFERENCES snapshots,
    source_id text,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    snapshot_id text REFERENCES snapshots,
    source_id text,
    tenant_id text NOT NULL,
    po_id text,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id),
    FOREIGN KEY (snapshot_id, po_id) REFERENCES purchase_orders
);

CREATE TABLE IF NOT EXISTS po_lines (
    snapshot_id text,
    source_id text,
    po_id text NOT NULL,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id),
    FOREIGN KEY (snapshot_id, po_id) REFERENCES purchase_orders
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    snapshot_id text,
    source_id text,
    invoice_id text NOT NULL,
    po_line_id text,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id),
    FOREIGN KEY (snapshot_id, invoice_id) REFERENCES invoices,
    FOREIGN KEY (snapshot_id, po_line_id) REFERENCES po_lines
);

CREATE TABLE IF NOT EXISTS receipts (
    snapshot_id text,
    source_id text,
    po_line_id text NOT NULL,
    reverses_receipt_id text,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id),
    FOREIGN KEY (snapshot_id, po_line_id) REFERENCES po_lines,
    FOREIGN KEY (snapshot_id, reverses_receipt_id) REFERENCES receipts
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS allocations (
    snapshot_id text,
    source_id text,
    invoice_id text NOT NULL,
    po_line_id text NOT NULL,
    position integer NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_id),
    FOREIGN KEY (snapshot_id, invoice_id) REFERENCES invoices,
    FOREIGN KEY (snapshot_id, po_line_id) REFERENCES po_lines
);

CREATE TABLE IF NOT EXISTS coverage (
    snapshot_id text REFERENCES snapshots,
    position integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, position)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    request jsonb NOT NULL,
    snapshot_id text NOT NULL REFERENCES snapshots,
    invoice_id text NOT NULL,
    policy_version text NOT NULL,
    state text NOT NULL CHECK (state IN ('RUNNING', 'RETRYABLE', 'COMPLETE')),
    lease_token text,
    lease_expires_at timestamptz,
    attempts integer NOT NULL CHECK (attempts > 0),
    reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (snapshot_id, invoice_id) REFERENCES invoices,
    CHECK ((state = 'RUNNING') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS runs_by_invoice ON runs (tenant_id, invoice_id, created_at);

CREATE TABLE IF NOT EXISTS cases (
    case_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    run_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('READY_FOR_REVIEW', 'APPROVED', 'RETURNED_FOR_RESEARCH')),
    version integer NOT NULL CHECK (version > 0),
    packet jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, run_id),
    UNIQUE (tenant_id, case_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    actor_id text NOT NULL,
    case_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('APPROVE_DISPOSITION', 'RETURN_FOR_RESEARCH')),
    reason text NOT NULL,
    version integer NOT NULL,
    FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, case_id),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id text NOT NULL REFERENCES cases,
    actor_id text NOT NULL,
    version integer NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('CASE_READY', 'REVIEW_DECIDED')),
    decision_id text REFERENCES decisions,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (case_id, version)
);
