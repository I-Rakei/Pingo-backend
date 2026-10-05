# Pingo backend

Django API for the Pingo web app and Dividas mobile app. SQLite is the ledger;
browser sessions use CSRF, native clients use DRF tokens. Protocol v2 adds a
transactional change log, HTTP sync, and a Channels WebSocket feed. The server
flag for mobile v2 defaults to off, and production has not yet moved to ASGI/Redis.

## Run locally

```powershell
cd "D:\ACODIGO\PINGO CORE\Pingo APP\backend"
py -m pip install -r requirements.txt
Copy-Item .env.example .env
py manage.py migrate
py manage.py createsuperuser
py manage.py runserver
```

The API is at `http://127.0.0.1:8000/api/`. Add the Vite origin to `CORS_ALLOWED_ORIGINS` before using cookie-based requests from the frontend. Fetch `/api/auth/csrf/` with `credentials: "include"`, send its token as `X-CSRFToken` for every write, and also use `credentials: "include"`.

For local WebSocket testing, start Redis at the `REDIS_URL` in `.env`, then run
`py -m uvicorn config.asgi:application --host 127.0.0.1 --port 8000` instead of
`runserver`. `/ws/ledger/` accepts a browser session with an allowed Origin
or a native `Authorization: Token <token>` handshake. It supports `hello`,
`changes`, `push`/`push_result`, `ack`, and `ping`/`pong`. The web client uses
change notices to debounce a fresh `/api/bootstrap/` request. The production
switch to ASGI/Redis is an operator rollout step; no deployment is implied here.

## Frontend routes

`GET /api/bootstrap/` returns `{ user, settings, clients, debts, payments }`. Dashboard consumers can use `GET /api/dashboard/summary/` and `GET /api/dashboard/debts/`. Other routes are `GET /api/auth/csrf/`, `POST /api/auth/register/`, `POST /api/auth/login/`, `POST /api/auth/logout/`, `GET/PATCH /api/auth/me/`, `GET/PATCH /api/preferences/`, `GET/POST /api/clients/`, `GET/POST/DELETE /api/debts/`, `GET/PATCH/DELETE /api/debts/:id/`, `POST /api/debts/:id/payments/`, and `POST /api/debts/:id/installments/:number/revert/`.

Web ledger output is camelCase. Debt `id` stays a display-compatible `PNG-...`
reference; `publicId`, `createdAt`, and `updatedAt` provide sync-safe identity
and change tracking. Payment and reversion responses return `{ debt, payments }`.
`GET/POST /api/clients/by-public-id/:uuid/share/` lets an authenticated mobile
client fetch or regenerate a public client profile link after syncing that client.

## Dividas SQLite import

Create the destination user first, then import its Expo database without changing the Dividas project:

```powershell
py manage.py import_dividas_sqlite "C:\path\to\dividas_v3.db" --user owner@example.com
```

The command recognizes the `clients`, `debts`, `installments`, and `payments` schema in `Dividas/lib/database.ts`, retains legacy IDs in sync fields, and is idempotent for the same user/database. It prints warnings for missing client links, missing related rows, reference collisions, and totals that do not reconcile.

## Mobile accounts and legacy v1 sync

Dividas remains offline-first: the phone keeps its `dividas_v3.db` ledger after
cloud migration. The user connects a personal Pingo account in Settings. Browser
session authentication is unchanged; native requests use the returned DRF token:

```http
POST /api/mobile/register/
Content-Type: application/json

{"email":"owner@example.com","password":"at-least-8","name":"Owner"}
```

`POST /api/mobile/login/` accepts the same `email` and `password` and returns the same shape. Both responses contain `{ "token", "user" }`. Send that token on uploads as `Authorization: Token <token>`.

```http
POST /api/mobile/sync/
Authorization: Token <token>
Content-Type: application/json

{
  "deviceId": "stable-app-generated-device-id",
  "deviceLabel": "Ana's phone",
  "batchId": "new-uuid-per-upload",
  "snapshot": {
    "clients": [{"localId":"1","name":"Ana","phone":"+258...","email":"","address":"","notes":"","createdAt":"2099-01-01"}],
    "debts": [{"localId":"9","clientLocalId":"1","debtorName":"Ana","amount":"100","interestRate":"10","durationMonths":1,"penaltyRate":"0","totalAmount":"110","dueDate":"2099-02-01","startDate":"2099-01-01","isPaid":false,"loanType":"multi","capitalRemaining":"100","createdAt":"2099-01-01"}],
    "installments": [{"localId":"12","debtLocalId":"9","installmentNumber":1,"baseAmount":"110","penaltyAmount":"0","totalAmount":"110","dueDate":"2099-02-01","isPaid":false,"paidAmount":"0"}],
    "payments": [{"localId":"14","debtLocalId":"9","installmentLocalId":"12","amount":"20","note":"","createdAt":"2099-01-15","type":"principal"}]
  }
}
```

`localId` is required for every row. Current raw SQLite `id`, `client_id`, `debt_id`, and `installment_id` names are accepted as compatibility aliases, but the app should send the explicit names above. IDs are namespaced by `deviceId`, then scoped to the authenticated user, preventing two phones or two accounts from colliding.

`POST /api/mobile/sync/` upserts the snapshot. Missing rows are not deletions;
explicit `deleted` UUID tombstones are processed in dependency order. The reply
includes `status`, inserted/updated counts, and `serverTime`. Repeating one
`deviceId` + `batchId` with identical data returns `already_processed`; reusing
the batch ID for different data is rejected. `GET /api/mobile/sync/` returns the
canonical account snapshot. V1 phones sync on startup, roughly every 30 seconds
while active, on reconnect/foreground, and on manual refresh. Keep this endpoint
for older APKs through the D5 compatibility window after v2 rollout.

## Protocol v2

V2 is available over the same scoped service through HTTP and `/ws/ledger/`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/sync/v2/config/` | Mobile rollout flag; false by default |
| POST | `/api/sync/v2/hello/` | Bind a device and report cursor status |
| GET | `/api/sync/v2/changes/` | Fetch ordered changes, at most 500 per page |
| GET | `/api/sync/v2/bootstrap/` | Recover from an expired cursor |
| POST | `/api/sync/v2/push/` | Apply an idempotent grouped mutation |

The upgraded phone finishes one v1 cycle and imports a canonical v2 bootstrap
before switching protocols. Local changes and an outbox entry commit in one
SQLite transaction. The foreground socket drains that outbox immediately, uses
cursor catch-up, and reconnects with backoff. After repeated socket failures,
background sync, or manual sync, it uses HTTP v2. Server revisions and field
merges handle editable data; protected or structural conflicts remain in the
mobile Settings inbox for re-apply or discard. Reversed payments stay as facts
in v2. The 30-second foreground timer remains only while a phone uses v1.

`PINGO_MOBILE_V2_ENABLED=false` keeps mobile discovery on v1 until the operator
enables a device-first rollout. Corporate accounts remain blocked on mobile.
V1 batches, tombstones, and the endpoint must remain until the post-rollout D5
window has elapsed and old APKs are accounted for.

## Client emails and browser push notifications

Generate one VAPID key for each deployed environment:

```powershell
py manage.py generate_vapid_keys
```

The private key is stored in the ignored `.secrets` directory by default. Set `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` in production. The React Settings page registers a browser subscription through `/api/push/subscription/` and can send a test through `/api/push/test/`.

Run the notification worker under the same process supervisor used for Django:

```powershell
py manage.py run_notification_worker
```

The worker checks every 60 seconds by default. Alternatively, schedule `py manage.py send_due_notifications` every minute.

Clients with a recorded email receive welcome, new-loan, payment-receipt, and full-payment emails for changes from web or mobile (both sync protocols). Emails are queued transactionally in the existing `PushDelivery` table and retried after delivery failures. No database migration is required. Configure `DJANGO_EMAIL_BACKEND` and the existing `EMAIL_*` settings for a real SMTP relay; the development default prints mail to the console. `EMAIL_TIMEOUT` defaults to 30 seconds.

A repayment produces one receipt with the total paid, a breakdown of its interest/principal or installment allocations, and the remaining balance. Web receipts group rows by payment operation; mobile receipts combine newly imported rows per debt in a sync snapshot or mutation. A final repayment includes the fully paid confirmation in that same email, including any earlier payment amounts still awaiting email delivery.

Client reminders include a notice the day before an unpaid installment and one combined overdue email per debt. Overdue emails repeat every 30 days after the last successful reminder while the debt remains overdue, and stop after payment. Owner browser-push preferences do not disable client emails.

Web Push requires HTTPS in production; localhost is accepted during development. Owner push notifications retain the day-before reminder and at most one overdue notification per installment per day, following each user's notification preferences.

## Verification

```powershell
py manage.py test ledger
py manage.py makemigrations --check --dry-run
```

Web lint/build and Dividas TypeScript, lint, SQLite contract, and socket tests
are documented in `MASTER_CONTEXT.md` section 13. Release APK upgrade, real
Redis/ASGI, two-browser-tab, phone-to-web latency, airplane-mode, and background
runtime checks remain operator rollout gates.
