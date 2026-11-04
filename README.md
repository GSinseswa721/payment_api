# Payment Callback API (Django + MySQL)

An API endpoint that receives payment notifications (callbacks) from a payment provider such as MTN, verifies they are genuine, stores each payment transaction, and updates the loan balance when a payment succeeds.

The focus is financial correctness: no double-counting of duplicate callbacks, no half-finished updates, and no corrupted records when a payment fails or the system crashes mid-way.

## Requirements covered

| Requirement | How it is met |
| --- | --- |
| Accept and validate incoming callbacks | `payments/views.py`: JSON parsing and field validation |
| Authenticate the provider | HMAC-SHA256 signature in the `X-Signature` header, with a per-provider secret |
| Appropriate HTTP response codes | 200, 400, 401, 409, 422, 500 (see the table below) |
| MySQL schema that stores every transaction | `Loan` and `Payment` tables (see Database design) |
| Prevent duplicates | Unique constraint on `(provider, transaction_id)` |
| Track status (pending, success, failed) | `Payment.status` field |
| Update the loan balance on success | `process_payment()` in `payments/services.py` |
| Payment record and balance update are atomic | One `transaction.atomic()` block with row locks |
| Failed transactions do not corrupt records | Failed payments are stored but never touch the loan |

## Tech stack

- Python and Django
- MySQL (InnoDB, which is required for transactions and row-level locking)

## Project structure

```
config/        Django project settings and root URLs
payments/
  models.py    Loan and Payment models
  views.py     Callback endpoint: parsing, signature check, validation
  services.py  process_payment(): the transactional business logic
  urls.py      Route for /callbacks/payments
  tests.py     Test suite
```

## Setup

1. Create a MySQL database and a user for it:
   ```sql
   CREATE DATABASE payment_api CHARACTER SET utf8mb4;
   ```
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your database credentials and provider secrets. Never commit `.env`.
4. Make sure `DATABASES` in `config/settings.py` uses the MySQL backend (`django.db.backends.mysql`) and reads those credentials.
5. Provider secrets are read from the `PROVIDER_SECRETS` setting, for example:
   ```python
   PROVIDER_SECRETS = {"MTN": "your-shared-secret"}
   ```
6. Run migrations and start the server:
   ```bash
   python manage.py migrate
   python manage.py runserver
   ```

## Running the tests

```bash
python manage.py test
```

The tests cover success, duplicates, failed payments, a simulated crash, conflicting references, overpayment, bad signatures, validation errors, bad JSON, and unknown loans.

## API

### `POST /callbacks/payments`

**Headers**

| Header | Description |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Signature` | Hex HMAC-SHA256 of the raw request body, using the provider's secret |

**Example request body**

```json
{
  "provider": "MTN",
  "transaction_id": "MTN20340",
  "loan_id": 1,
  "amount": 1000500,
  "currency": "RWF",
  "status": "success",
  "timestamp": "2026-10-05T14:30:00Z"
}
```

Field rules:

- `provider`: must have a configured secret
- `transaction_id`: required, non-empty string, unique per provider
- `loan_id`: positive integer of an existing loan
- `amount`: positive integer (RWF has no decimals)
- `currency`: `RWF`
- `status`: `success`, `failed` or `pending`
- `timestamp`: full ISO 8601 date and time

Note: the sample in the task brief has `status: "succes"` and `timestamp: "2026-10"`. These are rejected with a 422 on purpose. A misspelled status or an incomplete timestamp should never be silently accepted in a payment system.

**Response codes**

| Code | Meaning |
| --- | --- |
| 200 | Processed, or already processed (safe for the provider to retry) |
| 400 | Body is not valid JSON |
| 401 | Missing or invalid signature |
| 409 | Same `transaction_id` reused with a different amount or loan |
| 422 | Validation failed, or unknown loan |
| 500 | Unexpected error. Everything is rolled back and the provider should retry |

**Trying it with curl**

```bash
BODY='{"provider":"MTN","transaction_id":"MTN20340","loan_id":1,"amount":1000500,"currency":"RWF","status":"success","timestamp":"2026-10-05T14:30:00Z"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "your-shared-secret" | awk '{print $NF}')

curl -X POST http://127.0.0.1:8000/callbacks/payments \
  -H "Content-Type: application/json" \
  -H "X-Signature: $SIG" \
  -d "$BODY"
```

## Database design

### `Loan`

| Column | Type | Notes |
| --- | --- | --- |
| id | bigint, PK | |
| borrower_name | varchar(120) | |
| principal | unsigned bigint | Original loan amount |
| outstanding_balance | unsigned bigint | Reduced by successful payments |
| status | varchar(10) | `active` or `paid_off` |
| created_at | datetime | |

### `Payment`

| Column | Type | Notes |
| --- | --- | --- |
| id | bigint, PK | |
| provider | varchar(20) | For example `MTN` |
| transaction_id | varchar(64) | The provider's reference |
| loan_id | FK to Loan | `PROTECT`: a loan with payments cannot be deleted |
| amount | unsigned bigint | Amount received |
| overpaid_amount | unsigned bigint | Part of the amount that exceeded the balance |
| currency | varchar(3) | `RWF` |
| status | varchar(10) | `pending`, `success` or `failed` |
| provider_timestamp | datetime | When the provider says the payment happened |
| created_at / updated_at | datetime | Audit timestamps |

**Unique constraint:** `(provider, transaction_id)` stops the same transaction being stored twice. This is enforced by the database itself, not just by application code.

Amounts are stored as integers because RWF has no minor units, which avoids floating-point rounding errors.

## How it works

Processing a callback (`process_payment`) runs in a single database transaction:

1. Create the `Payment` row as `pending` if it does not exist (`get_or_create`).
2. Lock that payment row (`select_for_update`).
3. If the same reference arrives with a different amount or loan, return 409.
4. If the payment is no longer `pending`, return 200 and change nothing.
5. If the callback status is `pending`, keep it as is. If `failed`, mark the payment `failed` and leave the loan untouched.
6. If `success`, lock the loan row, subtract the amount (capped at the outstanding balance), set the loan to `paid_off` when it reaches zero, record any overpayment, and mark the payment `success`.

If anything fails, the whole transaction rolls back.

## Scenarios handled

**Same callback sent multiple times**
The unique constraint allows only one row per transaction. After the first is processed, later copies return `200 already processed` and change nothing. The balance is reduced once.

**Provider confirms payment but the database update fails**
The payment row, loan balance and status change are all in one transaction, so they commit together or not at all. A failure returns 500 and leaves no partial data. The provider retries and the retry succeeds from a clean state.

**Two payments for the same loan arrive at the same time**
The loan row is locked while the balance is updated, so the payments are applied one after the other, each reading the latest balance. No lost updates, and the balance never goes below zero. This relies on InnoDB row-level locking.

**Provider retries after a network timeout**
Treated like a duplicate. If the first attempt finished, the retry gets `already processed`. If it is still running, the retry waits on the payment row lock and then sees the finished result.

**Failed transactions**
A `failed` callback is stored with status `failed` and the loan is never modified. A later `success` for the same reference is ignored, so a failed record can never turn into a balance change.

**Overpayment**
Only the amount owed is applied. The excess is saved in `overpaid_amount`, and the loan is marked `paid_off`.

## Security

- Every request must carry a valid HMAC-SHA256 signature of the raw body. Signatures are compared in constant time.
- Each provider has its own secret, kept in configuration and not in code.
- Unknown providers and bad signatures get a 401 before any data is touched.

## Deployment

This is how the service would be prepared for production.

### 1. Configuration and secrets
- `DEBUG = False` and `ALLOWED_HOSTS` set to the real domain.
- `SECRET_KEY`, database credentials and `PROVIDER_SECRETS` come from environment variables, never from code. `.env` is not committed.
- Separate secrets per environment (dev, staging, production), with a plan to rotate provider secrets.

### 2. Database (MySQL)
- InnoDB tables and `utf8mb4` encoding.
- A dedicated database user with only the privileges the app needs.
- `CONN_MAX_AGE` set so connections are reused.
- Automated backups, with a restore tested before going live.
- Migrations run as a deploy step, before the new code serves traffic:
  ```bash
  python manage.py migrate
  ```

### 3. Running the app
`runserver` is for development only. In production the app runs under Gunicorn behind a reverse proxy:

```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 30
```

- Nginx (or a cloud load balancer) sits in front and terminates HTTPS. Providers call the callback over TLS only.
- Dependencies are pinned in `requirements.txt`, including `gunicorn` and the MySQL driver.

### 4. Containers
The app and MySQL can be run together with Docker and Docker Compose so every environment behaves the same. The container runs the Gunicorn command above and receives its configuration through environment variables.

### 5. Security hardening
- Allow only the provider's published IP ranges to reach the callback endpoint, in addition to the signature check.
- Rate limiting at the proxy.
- `SECURE_SSL_REDIRECT`, secure cookies and HSTS enabled.
- Secrets and full signatures are never written to logs.

### 6. Logging and monitoring
- Log the outcome of every callback (provider, transaction_id, result) and log unexpected errors with their stack trace.
- Alert on spikes of 5xx responses, repeated 401s (possible attack), and `failed` payments or 409 conflicts.
- A simple health-check endpoint for the load balancer.

### 7. Testing and CI/CD
- Run `python manage.py test` on every push against a real MySQL service.
- Add a two-thread concurrency test to prove the row locking.
- Deploy to staging first and send sandbox callbacks from the provider before going live.
- Run `python manage.py check --deploy` before each release to catch insecure settings.

### 8. Operations and safety nets
- **Reconciliation:** a daily job compares the provider's transaction report with the `Payment` table, so any callback that never arrived or never succeeded is caught.
- **Rollback:** keep the previous release available, and keep migrations backward compatible so code can be rolled back without breaking the database.
- **Scaling:** several instances can run behind the load balancer. This is safe because the unique constraint and row locks already handle duplicate and concurrent callbacks.

## Assumptions and limitations

- The provider retries on non-2xx responses. There is no reconciliation job for payments the provider never retries.
- A late `success` after a `failed` is ignored rather than applied. Decide whether this fits your provider's behavior.
- Only RWF is supported.
- There is no freshness check on `timestamp`; idempotency is what protects against replayed callbacks.
- The test suite runs each test in a transaction, so it does not prove true concurrent behavior. A two-thread `TransactionTestCase` against MySQL would be the next step.