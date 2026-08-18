# MillTrack API

FastAPI backend for MillTrack — raw material gate-in to dispatch traceability for a
spinning mill. Runs against the Postgres schema in `MillTrack_Database_Schema.sql`
(hosted on Supabase), plus the PIN-auth columns added to `identity.users`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in DATABASE_URL, JWT_SECRET, S3_BUCKET
```

## Run

```bash
uvicorn app.main:app --reload
```

- API base: http://localhost:8000
- Docs: http://localhost:8000/docs
- Health check: http://localhost:8000/api/v1/health

## Project structure

```
app/
  api/
    routes/          # one module per actor: auth, gate, godown, supervisor, packaging, admin
    router.py        # aggregates all routers under API_V1_PREFIX
  core/
    config.py        # settings via pydantic-settings
    db.py             # asyncpg pool + row-serialization helpers (rec/recs)
    security.py       # PIN hashing, JWT issue/verify
    deps.py            # get_current_user + require_role(*roles) dependency
    storage.py         # S3 presigned upload URLs
  main.py             # app entrypoint: lifespan (DB pool), CORS, Postgres error handler
```

## Design notes

- **Login is employee code + PIN**, not Cognito — `identity.users.pin_hash` is the
  source of truth. `POST /api/v1/auth/login` returns a JWT carrying `role` and
  `token_version`; every other route re-checks both against the DB on each request
  (see `deps.get_current_user`), so deactivating a user or calling
  `POST /admin/users/{id}/revoke` takes effect immediately, not just at token expiry.
- **Routers never recompute stock.** Every write endpoint inserts into the same
  tables the SQL schema's triggers already cascade from (bale → raw material ledger,
  mixing scan → ledger + lot state, DOFF → loose stock, packaging → loose stock debit
  + production stock credit, dispatch → production stock debit). The API's job is to
  insert the right root record and read back what the triggers produced — current
  stock is always `SUM(ledger)`, never cached or recalculated in Python.
- **Client-supplied UUIDs are idempotency keys.** Every field-capture endpoint (gate
  entries/exits, bales, mixing scans, DOFF, packaging, dispatch) accepts an `id` in
  the request body and upserts it as a no-op on conflict, so a retried offline-sync
  request can never create a duplicate.
- **Postgres errors are translated centrally**, in `main.py`'s exception handler —
  unique violations (double-exit, double-dispatch of a bag, a second ACTIVE mixing
  lot) → 409, CHECK violations (efficiency outside 0–100) → 422, trigger-raised
  business rules (dispatch against a non-LOADING vehicle, any UPDATE/DELETE attempt
  on an append-only table) → 400. Routers don't need their own try/except.
- **Operational reads vs. dashboard reads are deliberately different sources.**
  Live screens (unloading queue, godown inventory, mixing progress, loose stock)
  query the ledger/views directly for real-time correctness. `/admin/analytics/*`
  reads from the materialized views instead, which are fine to be a few minutes
  stale for a GM/MD dashboard but would be wrong to use anywhere operational.
- **Traceability (`/admin/trace/{code}`) is honest about where the chain degrades.**
  Forward from a bale, it resolves only to the mixing lot it was scanned into —
  once fibre is blended, finer resolution isn't physically meaningful (see the
  design doc's own §7). The DOFF→bag trace is similarly a fungible-pool match, not
  a strict one-to-one lineage.
- **Out of scope for this pass, by explicit direction:** alerting
  (`analytics.alert_configs`/`alert_events` exist in the schema but nothing
  evaluates them yet) and an automated test suite.
