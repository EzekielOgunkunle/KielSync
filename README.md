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
| Retryable/terminal error classification | Built |
| Payload redaction for logs | Built |
| Webhook conformance vectors | Built |
| Flutterwave adapter | Not started |
| Webhook HTTP endpoint | Not started |
| Reconciliation sweeper | Not started |
| Routing and gateway failover | Not started |

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

Credentials come from the environment, never from your settings module:

```bash
export KIELSYNC_PAYSTACK_SECRET_KEY="sk_live_..."
```

## Using the Paystack adapter

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

`verify()` reports the amount and currency *Paystack* gives, not the ones
you asked for. That is deliberate: comparing them is reconciliation work
and needs the originating transaction, which the adapter does not have. A
short payment is a mismatch for you to record, not an exception for the
adapter to raise.

Amounts are integers in the currency's minor unit throughout — kobo for
NGN, cents for USD, and whole units for zero-exponent currencies like
XOF. Nothing in the library converts them, so nothing can accidentally
multiply or divide a real charge by a hundred. Convert at your own edges:

```python
from kielsync.core.currency import to_minor_units, to_display

to_minor_units("5000.00", "NGN")   # 500000
to_display(500_000, "NGN")         # Decimal("5000.00")
```

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

`parse_webhook` takes the raw request body as bytes and verifies the
HMAC-SHA512 signature with `hmac.compare_digest` *before* parsing
anything. A naive `==` on digests short-circuits at the first differing
byte, and that timing difference is enough for someone submitting
repeated webhooks to recover a valid signature one byte at a time — which
is the same as being able to forge payment notifications.

```python
result = gateway.parse_webhook(request.body, request.headers)

if not result.signature_valid:
    return HttpResponse(status=400)      # every other field is empty
```

The body must reach this call as the exact bytes received. A JSON
round-trip on the way reorders keys and rewrites whitespace, and a
genuine signature will not survive it.

When verification fails, every other field on the result is empty. An
unauthenticated payload is never partially trusted, so a forged body
cannot smuggle an amount or a reference through by being rejected in a
way that still reports what it claimed.

Paystack sends no delivery identifier, so KielSync derives a
deduplication key from the event name and the transaction id
(`charge.success:302961`). The event name is included because one
transaction legitimately produces several events, and keying on the
transaction alone would drop the second as a duplicate of the first.

### Conformance vectors

[`tests/vectors/`](tests/vectors/) holds 18 webhook deliveries paired
with the exact result a correct implementation must produce, as
standalone JSON data files rather than fixtures embedded in Python. They
cover tampered bodies, missing, empty, truncated and non-ASCII signature
headers, and bodies that authenticate but do not decode. The intent is
that a second implementation — in another language, or your own — can be
checked against them without porting any test code. See the
[format documentation](tests/vectors/README.md).

## Architecture

The package is split in two, and the dependency runs one way only.

```
src/kielsync/
├── core/          pure Python — zero Django imports
│   ├── gateways/  base.py (protocol + dataclasses), paystack.py
│   ├── errors.py  classify() and the GatewayError hierarchy
│   ├── states.py  transition tables
│   ├── currency.py
│   ├── logging.py redact()
│   └── exceptions.py
└── django/        models, app config, migrations, settings
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
