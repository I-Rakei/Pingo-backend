# Pingo backend

Django REST API for the Pingo frontend. It uses SQLite, Django's built-in `User`, session cookies, CSRF protection, Django REST Framework, and `django-cors-headers`.

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

## Frontend routes

`GET /api/bootstrap/` returns `{ user, settings, clients, debts, payments }`. Dashboard consumers can use `GET /api/dashboard/summary/` and `GET /api/dashboard/debts/`. Other routes are `GET /api/auth/csrf/`, `POST /api/auth/register/`, `POST /api/auth/login/`, `POST /api/auth/logout/`, `GET/PATCH /api/auth/me/`, `GET/PATCH /api/preferences/`, `GET/POST /api/clients/`, `GET/POST/DELETE /api/debts/`, `GET/PATCH/DELETE /api/debts/:id/`, `POST /api/debts/:id/payments/`, and `POST /api/debts/:id/installments/:number/revert/`.

All ledger output is camelCase. Debt `id` stays a display-compatible `PNG-...` reference; `publicId`, `createdAt`, and `updatedAt` provide sync-safe identity and change tracking. Payment and reversion responses return `{ debt, payments }`.

## Dividas SQLite import

Create the destination user first, then import its Expo database without changing the Dividas project:

```powershell
py manage.py import_dividas_sqlite "C:\path\to\dividas_v3.db" --user owner@example.com
```

The command recognizes the `clients`, `debts`, `installments`, and `payments` schema in `Dividas/lib/database.ts`, retains legacy IDs in sync fields, and is idempotent for the same user/database. It prints warnings for missing client links, missing related rows, reference collisions, and totals that do not reconcile.

## Opt-in mobile migration and sync

Dividas remains offline-first: the phone keeps its SQLite data even after a successful cloud migration. The mobile app may continue offline, or a user can explicitly create/sign in to a Pingo cloud account from Settings and upload a snapshot. Browser session authentication is unchanged; native requests use the returned DRF token:

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

The endpoint only upserts uploaded records. It never deletes cloud or phone data because a row is absent from a snapshot. A successful reply contains `status`, per-collection `counts.inserted`/`counts.updated`, and `serverTime`. Repeating the same `deviceId` + `batchId` with identical data returns `status: "already_processed"` and makes no duplicate records; reusing a batch ID for different data is rejected. The app can safely retry an interrupted upload and schedule later uploads (for example, every hour while online) using a new batch ID each time.

## Browser push notifications

Generate one VAPID key for each deployed environment:

```powershell
py manage.py generate_vapid_keys
```

The private key is stored in the ignored `.secrets` directory by default. Set `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` in production. The React Settings page registers a browser subscription through `/api/push/subscription/` and can send a test through `/api/push/test/`.

Run the due-reminder scheduler under the same process supervisor used for Django:

```powershell
py manage.py run_notification_worker
```

Alternatively, schedule `py manage.py send_due_notifications` hourly. Web Push requires HTTPS in production; localhost is accepted during development. The worker sends one reminder the day before an unpaid installment and at most one overdue notification per installment per day, following each user's notification preferences.

## Verification

```powershell
py manage.py test
```
