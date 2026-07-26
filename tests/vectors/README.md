# Webhook conformance vectors

Each `*.json` file in this directory is one webhook delivery and the
result a correct implementation must produce for it. They are data, not
test code: `tests/test_webhook_vectors.py` is only a loader, and the same
files are meant to be runnable against any other implementation of the
KielSync webhook contract, in any language.

Nothing here is secret. The credentials are fixed, obviously fake values
and the payloads carry invented references, ids, and card fragments.

Vectors for both gateways live in this one directory. The `gateway` field
selects the adapter, not the filename; Flutterwave files happen to be
prefixed `flutterwave_` for readability only.

## Format

```json
{
  "name": "charge_success",
  "description": "Why this case exists and what it pins down.",
  "gateway": "paystack",
  "secret_key": "sk_test_kielsync_conformance_vector_key",
  "raw_body": "{\"event\":\"charge.success\", ...}",
  "headers": {"x-paystack-signature": "<hex sha512 hmac>"},
  "expected": { ... }
}
```

Flutterwave vectors carry an extra `webhook_secret_hash` field, because
Flutterwave authenticates webhooks with a shared secret that is a
*different* value from the API secret key:

```json
{
  "gateway": "flutterwave",
  "secret_key": "FLWSECK_TEST-kielsync-conformance-vector-key",
  "webhook_secret_hash": "kielsync-conformance-verif-hash",
  "headers": {"verif-hash": "kielsync-conformance-verif-hash"}
}
```

`raw_body` is the exact body as received, and the signature is computed
over its UTF-8 bytes. It must reach the parser without a JSON round-trip:
re-serialising reorders keys and rewrites whitespace, and the signature
will not survive it. Several vectors exist specifically to catch an
implementation that parses before verifying.

`expected` takes one of two shapes.

A parse outcome, with every field of `WebhookParseResult` stated
explicitly so that a field an implementation forgets to populate fails
rather than passes:

```json
{
  "signature_valid": true,
  "event_id": "charge.success:302961",
  "event_type": "charge.success",
  "gateway_reference": "kiel_txn_0001",
  "status": "SUCCESS",
  "amount": 500000,
  "currency": "NGN"
}
```

Or an error, for a delivery that authenticates but cannot be decoded:

```json
{"raises": "TerminalGatewayError"}
```

`amount` is always an integer in the currency's minor unit. `status` is
one of `SUCCESS`, `FAILED`, `PENDING`, or `null` when the signature did
not verify.

## The rejection cases

Every vector whose expected `signature_valid` is `false` also asserts
that *no* other field is populated. This is the property the whole
webhook path exists to guarantee: an unauthenticated payload is never
partially trusted, so a forged body cannot smuggle an amount or a
reference through by being rejected in a way that still reports what it
claimed.

## The two authentication schemes are not equivalent

Paystack signs the raw body with HMAC-SHA512. A valid signature is
evidence about the *payload*: change one byte and it stops verifying.
The `tampered_amount` and `tampered_whitespace_only` vectors pin that.

Flutterwave sends a static shared secret in `verif-hash`. It is the same
value on every delivery and is not computed over the body at all, so it
is evidence only about the *sender*. Anyone who has seen one valid header
can replay it against any payload they like.

`flutterwave_tampered_body_still_authenticates` exists to state that in
executable form. It carries an absurd forged amount and expects
`signature_valid: true`, which looks like a mistake until you know the
scheme. It is the reason KielSync's webhook handler calls `verify()`
independently before acting on any payload: for Paystack that is defence
in depth, but for Flutterwave it is the only real defence.

## Adding a vector

Compute the signature over the exact bytes you put in `raw_body`:

```python
import hashlib, hmac
hmac.new(secret_key.encode(), raw_body.encode(), hashlib.sha512).hexdigest()
```

`test_vector_signature_matches_its_declared_expectation` recomputes this
independently of the adapter, so a vector whose signature drifts out of
step with its body fails loudly instead of quietly asserting nothing.
