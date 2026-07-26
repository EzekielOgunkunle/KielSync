# KielSync

Payment orchestration and reconciliation for African payment gateways.

KielSync is a Django library for taking money reliably when the network,
the gateway, and the issuing bank are all allowed to fail. It keeps a
durable record of every payment attempt, classifies failures into ones
worth retrying and ones that are settled, and treats the gateway's own
`verify` call — never a webhook, never a browser redirect — as the only
evidence that money moved.

> **Status: pre-release, v0.1 in development.** The data layer and the
> Paystack adapter are built and tested. The orchestration on top of them
> is not. See [What exists today](#what-exists-today) before depending on
> any of this.

## Why

Most payment integrations are written against the happy path and then
patched, incident by incident, into something that survives contact with
production. The failures that cost money are boring and predictable:

- A read timeout on `initialize`. Did the charge happen? Retrying blindly
  may charge the payer twice; not retrying may lose a sale.
- A webhook that arrives twice, or out of order, or never.
- A gateway that returns HTTP 200 with `"status": false` and a decline
  reason buried three levels deep.
- A payment that succeeds for ₦4,999 against a ₦5,000 order.

KielSync's design assumptions follow from those: amounts are integers in
minor units and never floats, webhooks are evidence of nothing until
their signature verifies, a transaction becomes `SUCCESS` only after a
`verify()` call, and any failure the library does not positively
recognise is treated as terminal rather than retried.

## What exists today

| Component | Status |
| --- | --- |
| `Transaction` / `PaymentAttempt` / `WebhookEvent` models | Built |
| State machine with enforced transitions | Built |
| Currency minor-unit conversion | Built |
| `Gateway` protocol and result dataclasses | Built |
| Paystack adapter (initialize, verify, refund, webhook parsing) | Built |
| Flutterwave adapter (same, in major units) | Built |
| Retryable/terminal error classification | Built |
| Payload redaction for logs | Built |
| Webhook conformance vectors (both gateways) | Built |
| Webhook endpoint with deduplication | Built |
| Shared transaction resolution | Built |
| Reconciliation sweeper (`kielsync_sweep`) | Built |
| Routing and gateway failover | Not started (v0.2) |
| Circuit breakers | Not started (v0.2) |
| Reconciliation reporting | Not started (v0.3) |

The error classifier exists ahead of the failover logic it is for,
because retry policy is the thing that is expensive to get wrong later.

## Requirements

Python 3.12+, Django 5.x, httpx 0.27+. PostgreSQL for the models.

## Install

```bash
pip install -e ".[dev]"
```

Add the Django layer to your settings:

```python
INSTALLED_APPS = [
    ...,
    "kielsync.django",
]
```

Then `python manage.py migrate`. The app label is `kielsync`.

Mount the webhook receiver in your root URLconf:

```python
urlpatterns = [
    path("kielsync/", include("kielsync.django.urls")),
]
```

which serves `/kielsync/webhooks/<gateway>/`. Register that URL with each
gateway's dashboard.

Credentials come from the environment, never from your settings module:

```bash
export KIELSYNC_PAYSTACK_SECRET_KEY="sk_live_..."
export KIELSYNC_FLUTTERWAVE_SECRET_KEY="FLWSECK-..."
export KIELSYNC_FLUTTERWAVE_WEBHOOK_HASH="..."
```

`KIELSYNC_FLUTTERWAVE_WEBHOOK_HASH` is the "secret hash" from
Flutterwave's dashboard, which is a **different value** from the API
secret key. Conflating them is a common misconfiguration whose only
symptom is that every webhook fails authentication, which is why they are
read from separate variables.

Non-secret tuning goes in Django settings, where it is a normal
deployment concern rather than a credential:

```python
KIELSYNC_SWEEP_AFTER_MINUTES = 20    # grace period before verifying
KIELSYNC_ABANDON_AFTER_HOURS = 24    # write-off threshold
KIELSYNC_SWEEP_BATCH_LIMIT = 100
```

Finally, put the sweeper on cron — it is not optional, and the section
below explains why:

```cron
*/10 * * * * /path/to/python /path/to/manage.py kielsync_sweep
```

## Using an adapter

```python
from kielsync.core.gateways.base import InitializeRequest
from kielsync.django.settings import get_gateway

gateway = get_gateway("PAYSTACK")

result = gateway.initialize(
    InitializeRequest(
        amount=500_000,          # ₦5,000.00 in kobo — always minor units
        currency="NGN",
        email="payer@example.com",
        reference="kiel_txn_0001",
        callback_url="https://merchant.example/return",
    )
)
# Send the payer to result.authorization_url
```

When they come back, ask the gateway what actually happened:

```python
from kielsync.core.gateways.base import PaymentStatus

verification = gateway.verify("kiel_txn_0001")

if verification.status is PaymentStatus.SUCCESS:
    ...
```

`verify()` reports the amount and currency the *gateway* gives, not the
ones you asked for. That is deliberate: comparing them is reconciliation
work and needs the originating transaction, which the adapter does not
have. A short payment is a mismatch for you to record, not an exception
for the adapter to raise.

Swapping `"PAYSTACK"` for `"FLUTTERWAVE"` above changes nothing else. The
two APIs disagree about almost everything — including what a number
means — and absorbing that is the adapters' entire job.

### Amounts

Amounts are integers in the currency's minor unit throughout KielSync —
kobo for NGN, cents for USD, and whole units for zero-exponent currencies
like XOF, which have no subunit at all.

Paystack speaks minor units too, so that adapter never converts.
Flutterwave's v3 API speaks **major** units: it takes `5000` to mean
₦5,000 where Paystack takes `500000`. That conversion happens inside the
adapter, in both directions, through the currency exponent table — never
a hardcoded `/ 100`, which is wrong by a factor of a hundred across much
of West Africa.

Nothing outside `core/currency.py` does arithmetic on money. Convert at
your own edges:

```python
from kielsync.core.currency import to_minor_units, to_display

to_minor_units("5000.00", "NGN")   # 500000
to_display(500_000, "NGN")         # Decimal("5000.00")
```

Conversion refuses to round. An amount carrying more precision than the
currency can represent raises rather than quietly losing the remainder,
because the amount a gateway reports is compared against the amount a
transaction expects, and silently adjusting one side of that comparison
would defeat the check that exists to catch exactly that discrepancy.

## Failure classification

Every gateway failure is sorted into exactly one of two classes, and that
single decision governs what the orchestration layer is allowed to do
next.

- **Retryable** — nothing was decided. Connect and read timeouts, refused
  connections, HTTP 5xx, HTTP 429, and gateway-declared downtime such as
  `issuer_or_switch_inoperative`. Re-sending is safe.
- **Terminal** — something was decided, and the answer was no. Declines,
  insufficient funds, invalid cards and accounts, authentication
  failures, and any 4xx other than 429. Re-sending produces the same
  answer.

```python
from kielsync.core.errors import RetryableGatewayError, TerminalGatewayError

try:
    gateway.verify(reference)
except RetryableGatewayError:
    ...   # safe to try again, or on another gateway
except TerminalGatewayError:
    ...   # settled; do not retry
```

The asymmetry is the point. Misclassifying a terminal failure as
retryable produces duplicate charges against a payer who has already been
told no; misclassifying a retryable failure as terminal costs one
recoverable payment. So `classify()` resolves every uncertainty toward
terminal, and a signal it does not recognise is never retried.

Gateway codes outrank the HTTP status, because gateways routinely report
both declines and unreachable issuers inside an HTTP 200 envelope. Codes
are matched after case and separator folding, so `"Insufficient Funds"`
and `insufficient_funds` land in the same place.

## Webhooks

Mounting [`kielsync.django.urls`](src/kielsync/django/urls.py) gives you
`/kielsync/webhooks/<gateway>/`, one URL per gateway. The order of
operations inside is fixed, and each step is there because doing it later
is a known way to lose money:

1. Read the raw body as bytes, before anything parses it.
2. Authenticate. Nothing is stored, parsed, or trusted until this passes.
3. Deduplicate on `(gateway, event_id)`.
4. **Call `verify()` independently.**
5. Resolve under a `select_for_update` row lock.
6. Return 200 once the event is durably stored.

A rejected delivery returns 401 and **persists nothing** — storing
unauthenticated payloads would turn a public URL into an unbounded write.
A delivery already processed returns 200 immediately, without a second
`verify()` call or a second state change.

Step 4 is the one that matters. **The webhook is a notification, never a
source of truth.** A payload claiming a successful ₦5,000 payment moves
nothing until the gateway's own verification endpoint agrees, which is
what makes tampering pointless.

If processing fails, the endpoint still returns 200 with the event stored
`processed=False`, and the sweeper retries it. Returning non-200 only
asks the gateway to redeliver something that just failed
deterministically, which is how a redelivery storm starts.

### The two signature schemes are not equivalent

Paystack signs the raw body with HMAC-SHA512, compared with
`hmac.compare_digest`. A naive `==` short-circuits at the first differing
byte, and that timing difference is enough for someone submitting
repeated webhooks to recover a valid signature one byte at a time. A
valid Paystack signature is evidence about the *payload*: change one byte
and it stops verifying. The body must therefore arrive as the exact bytes
received — a JSON round-trip reorders keys and rewrites whitespace, and a
genuine signature will not survive it.

Flutterwave sends a static shared secret in a `verif-hash` header. It is
the same value on every delivery and is not computed over the body at
all, so it authenticates the *sender* and says precisely nothing about
the contents. Anyone who has seen one valid header can replay it against
any payload they like.

That asymmetry is why step 4 is not optional. For Paystack the
independent `verify()` call is defence in depth; for Flutterwave it is
the only real defence.

When authentication fails, every other field on the result is empty, so a
forged body cannot smuggle an amount or a reference through by being
rejected in a way that still reports what it claimed.

### Deduplication

Gateways retry, sometimes for days, so `WebhookEvent` is unique on
`(gateway, event_id)` and that column has to carry a stable value.

Paystack supplies a transaction id, so the key is the event name plus
that id (`charge.success:302961`). Flutterwave supplies no delivery
identifier at all, so KielSync derives one by hashing the event name, the
subject id, and the status. Status is part of the key because a
transaction reported pending and later successful is two events rather
than a redelivery; the event name is part of it because one transaction
legitimately produces a charge and later a refund, and keying on the
transaction alone would swallow the second.

### Conformance vectors

[`tests/vectors/`](tests/vectors/) holds 36 webhook deliveries — 18 per
gateway — paired with the exact result a correct implementation must
produce, as standalone JSON data files rather than fixtures embedded in
Python. They cover tampered bodies, missing, empty, truncated and
non-ASCII authentication headers, zero-exponent currency amounts, and
bodies that authenticate but do not decode. The intent is that a second
implementation — in another language, or your own — can be checked
against them without porting any test code. See the
[format documentation](tests/vectors/README.md).

One vector is deliberately counterintuitive:
`flutterwave_tampered_body_still_authenticates` carries an absurd forged
amount and expects `signature_valid: true`. That is not a mistake — it is
the executable statement of the weakness described above.

## Resolution and the sweeper

Every path that decides a payment's outcome ends in one function,
[`resolve_transaction`](src/kielsync/django/services.py). The webhook
handler calls it; the sweeper calls it; nothing else is allowed to mark a
transaction successful. Two code paths deciding the same thing is how
reconciliation bugs are born — they start identical, then one gets a fix
the other does not.

It compares the verified amount and currency against what the transaction
expects. **On any mismatch it does not mark success:**
`reconciliation_status` becomes `MISMATCHED`, the payment status is left
exactly as it was, and the discrepancy is logged at error level. A
payment that succeeded for the wrong amount is not a successful payment,
and deciding what to do about it needs a human. The function is
idempotent and never raises, from any combination of stored and reported
state, because a handler that raises becomes a redelivery storm.

### Why the sweeper is mandatory

Webhooks get lost. The gateway's retries give up, your endpoint is down
during the window, a deploy drops the request. Any integration that
treats webhooks as reliable eventually has customers who paid and were
never credited, and no way to find them.

```bash
python manage.py kielsync_sweep [--dry-run] [--gateway PAYSTACK] [--limit 100]
```

It asks the gateway directly about every payment still open: transactions
sitting in `PENDING` past the grace period, and webhook events stored
`processed=False`. Runs are bounded, ordered oldest first so nothing
starves under the batch limit, and locked with `skip_locked` so
overlapping cron runs cannot collide. No single item can abort the batch.

Transactions still pending past `KIELSYNC_ABANDON_AFTER_HOURS` are marked
`ABANDONED` — but only *after* verification, so a payment that settles at
hour 25 is rescued rather than written off.

Each run prints `examined / resolved / mismatched / abandoned / errored`.

## Architecture

The package is split in two, and the dependency runs one way only.

```
src/kielsync/
├── core/            pure Python — zero Django imports
│   ├── gateways/    base.py (protocol + dataclasses)
│   │                paystack.py, flutterwave.py
│   ├── errors.py    classify() and the GatewayError hierarchy
│   ├── states.py    transition tables
│   ├── currency.py  minor/major unit conversion
│   ├── webhooks.py  derived deduplication ids
│   ├── logging.py   redact()
│   └── exceptions.py
└── django/          models, migrations, app config
    ├── settings.py  the only module that reads os.environ
    ├── services.py  resolve_transaction() — the single decision point
    ├── views.py     the webhook receiver
    ├── urls.py
    └── management/commands/kielsync_sweep.py
```

Adapters never accept or return model instances; they exchange frozen
dataclasses. This keeps `core` testable without a framework and without a
database, and makes a test double for a gateway four plain methods rather
than a mock of an ORM object.

`core` reads no environment variables and no Django settings.
Configuration is injected by the caller, which is what allows two
adapters with different credentials to exist in one process.
`kielsync/django/settings.py` is the only module that reads `os.environ`.

The boundary is enforced, not just documented:
[`tests/test_core_boundary.py`](tests/test_core_boundary.py) walks the AST
of every module under `core/` and fails on any import of `django` or
`kielsync.django`, with a self-test proving the detector fires on the
imports it claims to catch.

## Security

- **Secrets never appear in logs, exception messages, or reprs.**
  `PaystackGateway.__repr__` redacts its key, so a live credential cannot
  reach a traceback or a debugger frame.
- **All gateway payloads pass through `redact()` before logging.** It
  masks authorization blocks, card data, BINs, last4, account numbers,
  signatures, and any key containing `key`, `secret`, or `token`, through
  nested dicts and lists. A sensitive key has its whole subtree masked
  rather than being descended into, so a field the gateway adds later is
  withheld by default.
- **TLS verification cannot be disabled.** There is no `verify` argument
  on the adapter's constructor, and a test asserts there never is one.
- **Explicit timeouts**: 5s connect, 30s read. httpx's 5s default is too
  short for a card authorisation waiting on an issuing bank, and no
  timeout at all can pin a worker until the process restarts.
- **No credential has a default.** A missing `KIELSYNC_PAYSTACK_SECRET_KEY`
  raises rather than falling back — a placeholder key surfaces later as
  confusing 401s, and a test key as payments that silently never settle.

## Development

Tests need PostgreSQL on `localhost:5433`:

```bash
docker run -d --name kielsync-pg \
  -e POSTGRES_USER=kielsync -e POSTGRES_PASSWORD=kielsync \
  -e POSTGRES_DB=kielsync -p 5433:5432 postgres:16-alpine

pytest
```

Connection details are overridable via `KIELSYNC_TEST_DB_*` environment
variables; see [`tests/settings.py`](tests/settings.py).

No test makes a live network call. Adapter tests run over
`httpx.MockTransport`, which sits below the client, so URL construction,
headers, timeouts, and JSON encoding are exercised for real while nothing
leaves the process. No real key is needed or used.

## License

MIT. See [LICENSE](LICENSE).
