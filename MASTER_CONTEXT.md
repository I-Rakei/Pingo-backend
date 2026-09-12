# Pingo Master Context

Last audited: 2026-09-12

This is the primary handoff document for Pingo. Read it before changing the
backend, web frontend, or Dividas mobile application. It records the product
intent, the implementation that exists today, the contracts between the three
applications, deployment details, and the invariants that must survive future
work.

The code remains the final authority. Where an older README conflicts with this
document, inspect the named source files before making a decision. In particular,
the backend README contains an older description of mobile sync; the current
implementation is bidirectional and supports explicit deletion tombstones.

## 1. Non-negotiable rules

1. Every cloud ledger row belongs to one authenticated Django user. One account
   must never be able to read, update, delete, export, or sync another account's
   clients, debts, installments, payments, preferences, devices, or push data.
2. Dividas is offline-first. Creating an account, logging in, updating the app,
   disconnecting cloud sync, losing connectivity, or encountering a failed sync
   must not erase data stored on the phone.
3. Cloud migration is opt-in. A person can continue using the mobile app without
   a server account indefinitely.
4. The first cloud sync must merge existing phone records into the user's cloud
   account. It must not replace the phone database with an empty server snapshot.
5. Once the same account is connected, desktop and mobile operate on the same
   logical ledger. Mobile pulls desktop changes and pushes phone changes.
6. Keep financial calculations centralized and behaviorally consistent. On the
   server, use `ledger/services.py`; do not independently invent balance logic in
   views or the web frontend. Changes also need an equivalent mobile SQLite path.
7. Never change the mobile database filename, Android package, URL scheme, or
   existing migration strategy casually. The app is branded Pingo, but installed
   versions still rely on `dividas_v3.db`, `com.dividas.app`, and the `dividas`
   scheme. Renaming them can strand existing users' data or install a second app.
8. Sync deletion is explicit. A row missing from an uploaded snapshot is not by
   itself a delete. Use tombstones.
9. Do not commit production secrets, the SQLite cloud database, VAPID private
   keys, session cookies, mobile auth tokens, or Cloudflare tunnel tokens.
10. Before editing, run `git status` in all three repositories. They are separate
    Git repositories and may have independent uncommitted work.

## 2. What Pingo is

Pingo is a private debt and loan ledger. It tracks people who owe money, the
terms of each debt, installment schedules, payments, outstanding balances,
overdue status, audit history, preferences, reminders, and downloadable
invoice/receipt records.

The system has three cooperating applications:

```text
Pingo web (React/Vite) ---- session + CSRF ----+
                                               |
                                               v
                                      Django REST API
                                               |
                                               v
                                      cloud SQLite database
                                               ^
                                               |
Dividas/Pingo mobile ---- DRF token + sync ----+
        |
        +---- local SQLite database (offline source of work)
```

The Django service is the canonical cloud ledger. The web application reads and
writes it directly. Mobile remains fully usable against its own SQLite database,
then reconciles that database with Django whenever cloud migration is enabled and
the device has connectivity.

This is not a public multi-tenant collaboration product. It is currently intended
for the owner's private use, but account isolation is still a hard security and
data-integrity boundary.

## 3. Repository layout

The workspace root is:

```text
D:\ACODIGO\PINGO CORE
```

The three repositories are:

| Application | Local path | Git remote |
| --- | --- | --- |
| Django API | `Pingo APP/backend` | `https://github.com/I-Rakei/backend.git` |
| React web | `Pingo APP/frontend` | `https://github.com/I-Rakei/Pingo-frontend.git` |
| Expo mobile | `Dividas` | `https://github.com/I-Rakei/Dividas.git` |

All were on branch `master` and clean at the last audit. The audited commits were:

```text
backend:  71eaa1d feat: Add client detail view with update functionality scoped to authenticated user
frontend: 705c793 feat: remove eyebrow text from PageHeader in multiple components for cleaner UI
mobile:   5b07cb1 feat: implement cloud sync functionality with refresh control across multiple screens
```

Do not assume that state is still current. Check it:

```powershell
git -C "Pingo APP/backend" status
git -C "Pingo APP/frontend" status
git -C "Dividas" status
```

## 4. Technology stack

### Backend

- Python and Django 5.x
- Django REST Framework 3.x
- Django's built-in `User`, sessions, CSRF, and DRF token authentication
- `django-cors-headers`
- SQLite at `backend/db.sqlite3`
- `pywebpush` with VAPID keys
- Gunicorn and WSGI in production
- PM2 supervises Gunicorn and the notification worker
- Cloudflare Tunnel publishes the private local Gunicorn port

There is no Flask, PostgreSQL, Redis, Celery, Django Channels, WebSocket server,
or ASGI application in the current architecture.

### Web frontend

- JavaScript, not TypeScript
- React 19.2
- Vite 8
- Tailwind CSS 4
- shadcn `base-nova` components built on `@base-ui/react`
- Iconify for application icons
- Lucide remains inside some generated shadcn primitives
- Inter Variable for all web typography
- Static PWA manifest and a manually registered service worker
- Vercel deployment

Do not introduce IBM Plex or IBM fonts. The product direction is Inter everywhere
on the web. The web application is dark-mode-only.

### Mobile

- Expo SDK 54 and Expo Router 6
- React Native 0.81.5 and React 19.1
- TypeScript 5.9 in strict mode
- `expo-sqlite` for the local ledger
- `expo-secure-store` for the cloud auth token
- AsyncStorage for sync metadata
- NetInfo for connectivity state
- Expo Background Task and Task Manager
- Expo Notifications for local mobile reminders
- Expo File System, Print, and Sharing for documents
- EAS Build for Android APKs

## 5. Backend architecture

The Django project is deliberately compact:

```text
backend/
  config/
    settings.py          environment, database, DRF, CORS, VAPID
    urls.py              `/admin/` and `/api/`
    wsgi.py              Gunicorn entry point
  ledger/
    models.py            persistent domain model
    serializers.py       API validation and response shapes
    services.py          debt creation, payments, reversals, calculations
    views.py             auth and HTTP endpoints
    mobile_sync.py       bidirectional snapshot merge protocol
    push.py              web push delivery helpers
    urls.py              API route table
    tests.py             backend behavioral and isolation tests
    management/commands/
      generate_vapid_keys.py
      import_dividas_sqlite.py
      run_notification_worker.py
      send_due_notifications.py
  manage.py
  requirements.txt
```

### Settings and environment

`config/settings.py` reads `backend/.env` itself, one `KEY=value` line at a
time. It does not depend on `python-dotenv`. The important variables are:

```dotenv
DJANGO_SECRET_KEY=replace-with-a-long-random-value
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=*
CORS_ALLOWED_ORIGINS=https://pingo.rakei.co.za
TIME_ZONE=Africa/Johannesburg
VAPID_PRIVATE_KEY=.secrets/vapid_private.pem
VAPID_SUBJECT=mailto:admin@example.com
```

The deployment currently prefers `DJANGO_ALLOWED_HOSTS=*` because it is private
use behind a tunnel. That works, but it broadens the accepted Host header surface.
If this becomes a public service, restrict it to the actual hostname.

`CORS_ALLOWED_ORIGINS` also becomes `CSRF_TRUSTED_ORIGINS`. Origins must be exact,
including scheme and port. Session cookies are sent cross-origin with
`CORS_ALLOW_CREDENTIALS=True`.

The ignored runtime files are:

```text
.env
db.sqlite3
.secrets/
```

### Authentication

The web uses Django sessions and CSRF protection. The frontend first obtains a
CSRF cookie/token at `/api/auth/csrf/`, includes credentials on every request,
and sends `X-CSRFToken` on writes.

Mobile uses DRF Token Authentication. Its token is stored in SecureStore, not in
SQLite or plain AsyncStorage. Registration and login normalize email to lowercase;
the Django username is the email address.

Default DRF permissions are `IsAuthenticated`. Only the explicit CSRF, login,
registration, mobile authentication, and public push configuration endpoints are
accessible before authentication.

`AUTH_PASSWORD_VALIDATORS` is currently empty and the application only enforces a
minimum eight-character password. This is acceptable for the present private-use
scope but should be hardened before inviting other people.

### Account isolation

All user-owned detail and collection queries must include `owner=request.user` or
an equivalent relationship-scoped filter. A globally unique `reference`, UUID,
primary key, endpoint, or device ID is never sufficient authorization.

The existing client detail implementation demonstrates the correct pattern:
lookup by both the route identifier and authenticated owner, returning 404 for a
missing or cross-account object. Preserve the same rule in exports, push actions,
bulk deletion, sync lookup, and admin-like endpoints.

## 6. Backend data model

`TimeStampedPublicModel` supplies these fields to synchronized entities:

- `public_id`: stable UUID, unique and indexed
- `created_at`: server creation timestamp
- `updated_at`: server modification timestamp

The integer Django primary key is an internal database identity. Sync uses public
UUIDs so records can survive local integer-ID differences.

### Client

- `owner`: owning Django user
- `legacy_id`: optional imported identifier
- `name`: required, maximum 160 characters
- `phone`: optional
- `email`: optional
- `address`: optional; this is the field exposed as `morada` in the product idea
- `notes`: optional
- `mobile_device` and `mobile_local_id`: origin identity for offline migration

`(mobile_device, mobile_local_id)` is unique. A client cannot be deleted while
protected debts or payments still reference it.

### Preference

One-to-one with a user. Current fields are language, currency (default `MZN`),
reminders, and overdue alerts.

### Debt

- belongs to an owner and a protected client
- globally unique generated reference such as `PNG-1001`
- loan type: `multi` or `single`
- principal and remaining capital
- interest and penalty percentages
- duration in months
- total, outstanding, and collected amounts
- start date and due date
- stored status plus dynamically computed current status
- optional mobile origin identity

`current_status` is paid when outstanding is zero, overdue when the due date is
before the server's local date, otherwise unpaid.

### Installment

- belongs to a debt and cascades with it
- unique period number within the debt
- due date
- total amount, base amount, penalty amount, and paid amount
- optional mobile origin identity

### Payment

- belongs to owner, debt, and client
- optionally points to an installment
- uses `operation_id` to group rows created by one atomic action
- amount, note, type (`principal` or `interest`), and payment date
- `reversed_at` implements soft reversal
- optional mobile origin identity

Normal API and sync snapshots exclude reversed payments. Reversal preserves an
audit trail instead of physically deleting the payment row.

### Sync and push models

- `MobileDevice`: binds a stable app-generated `device_id` to exactly one user.
- `MobileSyncBatch`: stores device, batch ID, payload hash, status, and counts so
  retries cannot duplicate data.
- `WebPushSubscription`: stores a user's browser endpoint and Web Push keys.
- `PushDelivery`: deduplicates scheduled events by `(owner, event_key)`.

## 7. Financial rules

Money is quantized to two decimal places using `ROUND_HALF_UP`. Month arithmetic
clamps dates to the last valid day in the destination month.

### Reference generation

`next_reference()` scans existing `PNG-N` references and returns the next number,
starting after 1000. This is simple but not safe against simultaneous writers in
multiple Gunicorn workers. If write concurrency increases, replace it with a
database-backed sequence or retry on unique constraint failure.

### Debt creation

A debt can reference an existing account-scoped client or create a minimal client
from the supplied name. The explicit client form supports name, phone, email,
address, and notes.

Interest is currently calculated once as:

```text
interest = principal * interest_rate / 100
total = principal + interest
```

For `multi` loans, total is divided over the duration and installment dates count
backward so the last installment ends on the debt due date.

For `single` loans, the installment represents periodic interest while principal
remains separately payable.

### Multi-period payments

The accepted action is `balance`. An optional installment number can target one
period; otherwise the payment is allocated through open installments in order.
The server updates installment paid amounts, collected total, outstanding total,
and status in one database transaction.

### Single-loan payments

- `interest`: pays the current interest period, creates the next month's interest
  installment, and advances the debt due date.
- `principal`: reduces remaining capital and recalculates open interest.
- `settle`: pays open interest and all remaining principal. The server creates two
  payment rows under one operation ID and closes the debt.

### Reversal

Reverting an installment marks matching payment rows as reversed and rebuilds the
derived totals. A single loan requires later periods to be reversed first; later
unpaid generated periods are removed and the prior due/interest position is
restored.

All balance-altering server operations are transactional. Preserve row locking on
payment paths.

## 8. HTTP API

All routes are under `/api/`. JSON intended for the web uses camelCase. A debt's
web-facing `id` is its reference string; a client's current web-facing ID is its
integer database ID. Sync identities are UUID strings.

### Public endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/auth/csrf/` | Establish browser CSRF state |
| POST | `/api/auth/login/` | Browser session login |
| POST | `/api/auth/register/` | Browser account creation |
| POST | `/api/mobile/register/` | Create account and return mobile token |
| POST | `/api/mobile/login/` | Return mobile token |
| GET | `/api/push/config/` | Return public VAPID key/support data |

### Authenticated endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/auth/logout/` | End browser session |
| GET, PATCH | `/api/auth/me/` | Read/update account profile |
| GET, PATCH | `/api/preferences/` | Read/update preferences |
| GET | `/api/bootstrap/` | Canonical web workspace snapshot |
| GET | `/api/dashboard/summary/` | Dashboard totals |
| GET | `/api/dashboard/debts/` | Dashboard debt list |
| GET, POST | `/api/clients/` | List/create clients |
| GET, PATCH | `/api/clients/<id>/` | Read/update an owned client |
| GET, POST, DELETE | `/api/debts/` | List/create or clear owned debt history |
| GET, PATCH, DELETE | `/api/debts/<reference>/` | Owned debt detail/update/delete |
| POST | `/api/debts/<reference>/payments/` | Record a payment action |
| POST | `/api/debts/<reference>/installments/<number>/revert/` | Reverse a period |
| GET | `/api/payments/` | List active owned payments |
| GET, POST | `/api/mobile/sync/` | Pull or merge a mobile snapshot |
| GET, POST, DELETE | `/api/push/subscription/` | Manage browser subscriptions |
| POST | `/api/push/test/` | Send a test browser notification |

`/api/bootstrap/` returns the signed-in user, preferences, clients, debts,
installments embedded with debts, and active payments. The web app treats this as
its canonical refresh payload.

## 9. Bidirectional mobile sync

The authoritative server implementation is `ledger/mobile_sync.py`. The mobile
orchestrator is `Dividas/lib/cloud-sync.ts`; serialization and merge logic live in
`Dividas/lib/database.ts`.

### Sync sequence

When connected, mobile performs this sequence:

1. Read token and enabled state. If not connected, do nothing.
2. Confirm network reachability.
3. `GET /api/mobile/sync/?deviceId=<stable-id>` with `Authorization: Token ...`.
4. Import the canonical cloud snapshot into phone SQLite.
5. Export the resulting merged phone database plus deletion tombstones.
6. Generate a unique batch ID.
7. `POST /api/mobile/sync/` with device ID, batch ID, schema version, and snapshot.
8. The server upserts the payload, applies tombstones, reconciles derived debt
   values, and returns its new canonical snapshot.
9. Mobile imports the canonical response.
10. Only after success, mobile clears local deletion tombstones and records the
    successful timestamp.

An in-memory `activeSync` promise deduplicates overlapping sync requests.

### Snapshot identity

Each synchronized client, debt, installment, and payment includes:

- `serverId`: stable Django `public_id` UUID when known
- `localId`: phone SQLite identity
- `createdAt` and `updatedAt`
- type-specific values and relationship identities

Relationships in uploads use local relationship IDs (`clientLocalId`,
`debtLocalId`, and `installmentLocalId`) because the first upload may not yet have
server UUIDs for every row.

The server matches a row in this order:

1. Valid `serverId` belonging to the authenticated user.
2. The authenticated device plus `mobile_local_id` fallback.

A UUID belonging to another user is never accepted as a match. A stable device ID
already linked to another account cannot be claimed.

### Conflict behavior

Existing rows use row-level last-write-wins based on `updatedAt`. Mobile overwrites
an existing server row only when its timestamp is later. Missing or invalid mobile
timestamps are treated as mobile-newer for migration compatibility.

This is not a field-level merge. Two devices editing different fields on the same
row at nearly the same time can still lose one edit. Device clock skew can also
influence the winner because phone timestamps originate from its local clock.
There is no vector clock, sync version, or per-field conflict UI today.

### Idempotency

The server hashes canonical snapshot JSON and stores each `(device, batch_id)`.
Retrying the same batch with the same hash returns `already_processed`; reusing a
batch ID with a different payload is rejected.

### Deletions

The phone stores tombstones in `sync_deletions`. Uploads apply them in dependency
order: payments, installments, debts, then clients. All server lookups remain
owner-scoped. Protected client deletion is rejected while related rows remain.

Missing uploaded rows are never interpreted as deletion. Conversely, when mobile
imports a canonical snapshot, a local row that already has a `server_id` but no
longer exists on the server is removed as a cloud deletion. Unsynced local rows,
which have no server ID, are always preserved.

### First migration safety

The SQLite schema uses additive migrations. Legacy rows receive `updated_at`
values so the phone's existing records win their initial merge and are uploaded.
The first pull does not remove unsynced phone rows. A failed fetch or POST leaves
the phone ledger and pending tombstones intact.

This is the core guarantee behind "data on the user's phone must not disappear."
Any sync rewrite needs regression tests for it.

### Schedule and freshness

Foreground mobile sync is attempted:

- at scheduler startup
- every 30 seconds while the app is active
- when network connectivity returns
- when the app becomes active
- immediately on pull-to-refresh in the main data screens

Successful sync listeners reload screen data from SQLite, so desktop-created rows
appear on mobile after the next successful cycle.

The background task requests a minimum interval of 15 minutes. Android and iOS
control actual execution, so it is inexact and not guaranteed while the app is
closed. The 30-second promise applies only while the app is active. There are no
WebSockets, and that is an intentional current decision.

Important desktop limitation: the web app does not currently poll the server. It
refreshes after its own writes and on initial load. If mobile changes the cloud
ledger while a desktop tab remains open, that tab needs a browser refresh or a
subsequent local mutation before it fetches the new snapshot.

## 10. Web frontend architecture

The frontend is a single Vite React application. Important files are:

```text
frontend/src/
  App.jsx                       global state, auth gate, view selection, mutations
  main.jsx                      React root and service worker registration
  index.css                     dark tokens, Inter, layout and loading styles
  lib/api.js                    fetch, sessions, CSRF, API methods, error handling
  lib/pingo-data.js             formatting helpers and legacy/sample helpers
  lib/push.js                   browser Push API integration
  components/
    pingo-sidebar.jsx           desktop/mobile navigation
    dashboard.jsx
    debts.jsx
    debt-profile.jsx
    clients.jsx
    client-profile.jsx
    history.jsx
    settings.jsx
    login.jsx
    add-debt.jsx
    notification-center.jsx
    data-pagination.jsx
    page-header.jsx
    ui/                          generated/custom shadcn primitives
public/
  manifest.webmanifest
  pingo-sw.js
  pingo-logo.png
```

### Auth and state

`App.jsx` calls `api.bootstrap()` at startup. While it is resolving, a full shell
skeleton is rendered. If the session is absent, only the login/register experience
is shown. Every application view is therefore behind authentication.

The app stores workspace data in React state. It does not use React Query, Redux,
or a browser ledger database. Mutations call the API and then `refreshWorkspace()`
to replace local state from `/api/bootstrap/`.

`lib/api.js` dispatches `pingo:unauthorized` on an authenticated request receiving
401 or 403. `App.jsx` clears the in-memory workspace and returns to login.

### Navigation

Navigation is state-based, not React Router. `activeItem` selects Dashboard,
Debts, Clients, History, or Settings; profile IDs are held separately. There is no
stable URL for every page. Notification deep links can use `?debt=PNG-...`; the
app opens that debt and then removes the query parameter.

The sidebar contains:

- Dashboard
- Debts
- Clients
- History
- Settings

Add Debt is a global modal/dialog and is intentionally absent from the sidebar.
The sidebar Pingo logo is 48px while expanded and 32px while collapsed.

### Current screens and workflows

Dashboard shows account portfolio totals and recent debt activity. Debt row menus
use three dots rather than arrows and expose the appropriate profile, payment,
document, edit, and delete actions.

Debts provides filtering, ten-row pagination, add/edit/delete, payment actions,
invoice or receipt download, and navigation to a debt's own profile. The debt
profile is distinct from the client profile and includes its schedule and payment
history.

Clients provides ten-row pagination, creation, editing, and profile navigation.
A client requires a name; phone, email, address, and notes are optional. Client
editing is currently a desktop/web feature.

History is an audit page, not an alternate debt detail screen. It has debts and
payments tabs, filters, CSV export, ten-row pagination, and a user column. Row
arrows were removed.

Settings provides profile/preferences, browser push setup/test, data download,
debt-history clearing, and sign out. The old Appearance card was removed because
the web application is dark-only.

Login and registration use the product image on the left on large screens, with a
black gradient at the image bottom. Form autocomplete is disabled to prevent the
browser from injecting stale values. Async actions use spinners rather than raw
"Loading..." or "Saving..." labels.

### Design system rules

- Dark mode only; do not add a light theme switch.
- Use Inter for all web text, including mono-like data.
- Core colors include background `#141518`, sidebar `#0d0e10`, card `#1c1e22`,
  and Pingo orange `#ff6c37`.
- Use shadcn components and existing Base UI composition patterns.
- Base UI menu labels/groups must remain inside their required group context.
- Use Iconify for application icons and tooltips for unfamiliar icon-only buttons.
- Keep operational pages dense, calm, and easy to scan.
- Do not add decorative eyebrow labels such as "Portfolio", "12 clients", "Ledger
  archive", or "Workspace". Those were explicitly removed.
- Display currency as a suffix, for example `59,800.00 MZN`.
- Tables and repeated ledger lists show ten rows per page.
- Avoid nested cards, decorative orbs, oversized marketing headers, and rounded
  text pills when a familiar icon is the right control.

### Web PWA and browser push

`main.jsx` manually registers `/pingo-sw.js`. The service worker handles Push API
events and notification clicks. `manifest.webmanifest` is a static file using the
Pingo logo, standalone display mode, and dark colors.

There is intentionally no `vite-plugin-pwa` runtime endpoint. Reintroducing its
synthetic entry path without installing/configuring the plugin recreates the old
404 and manifest errors.

Browser push can arrive while the web page is closed if the browser, operating
system, HTTPS context, and notification permissions allow it. This is separate
from Expo local notifications on the native app.

### Web scalability limits

- Bootstrap returns the entire account ledger; filtering and pagination are
  client-side. Server pagination will be needed for large datasets.
- There is no live desktop polling or WebSocket subscription.
- Navigation is not URL-addressable except for debt notification query links.
- The current production bundle reports a chunk larger than 500 kB after
  minification. Code splitting is a future optimization, not a functional blocker.
- `pingo-data.js` may contain legacy sample helpers. Do not treat sample arrays as
  live data; the backend bootstrap payload is the source of truth.

## 11. Dividas/Pingo mobile architecture

The mobile repository and much of its internal naming remain `Dividas`, while the
display name is Pingo. Its core paths are:

```text
Dividas/
  app/_layout.tsx               providers, database init, sync schedulers
  app/app-shell.tsx             onboarding versus authenticated app shell
  app/onboarding.tsx
  app/(tabs)/_layout.tsx        drawer navigation
  app/(tabs)/index.tsx          dashboard
  app/(tabs)/add-debt.tsx
  app/(tabs)/clients.tsx
  app/(tabs)/history.tsx
  app/(tabs)/settings.tsx
  app/client/[id].tsx
  app/debt/[id].tsx
  components/cloud-migration-card.tsx
  lib/database.ts               schema, migrations, ledger operations, sync import/export
  lib/cloud-sync.ts             cloud auth and sync scheduling
  lib/notifications.ts          native local notification scheduling
  lib/invoice.ts                document generation/sharing
  lib/settings-context.tsx
  lib/app-theme-context.tsx
  constants/theme.ts
  app.json
  eas.json
```

The navigation currently uses an Expo Router drawer with Dashboard, Add Debt,
Clients, History, and Settings. Client and debt profiles are dynamic routes.

### Stable native identifiers

Do not change these without a deliberate installed-data migration plan:

```text
display name: Pingo
Expo slug: pingo
Android package: com.dividas.app
URL scheme: dividas
SQLite filename: dividas_v3.db
EAS project ID: 3ad8bd75-8816-4b3a-9585-663837ebdd7d
```

Android uses portrait orientation, edge-to-edge mode, the new architecture, and
`softwareKeyboardLayoutMode: resize`. The resize setting plus keyboard-aware
auth/modal layout prevents login inputs from remaining hidden behind the keyboard.

### Local database

Mobile SQLite contains clients, debts, installments, payments, and
`sync_deletions`. Rows use local numeric IDs and gain nullable `server_id` plus
timestamps through additive migrations.

Schema upgrades call `addColumnIfMissing`; they do not drop and recreate the
ledger. Partial unique indexes prevent duplicate non-null server UUIDs. Timestamp
triggers fill or touch `updated_at` during writes.

The phone implements local debt creation, client management, penalty handling,
payments, reversals, settlement, invoice generation, and history queries. Changes
to server financial behavior need matching updates and tests here so an offline
operation has the same result after synchronization.

### Cloud migration UI

Settings renders `CloudMigrationCard`. A user can create a cloud account with
name/email/password or sign into an existing account. Connecting stores the token
and enables sync; disconnecting removes cloud credentials and pending auth state,
but does not delete the phone ledger.

Cloud migration is disabled on Expo web because SecureStore/native behavior is the
supported target. The desktop React application is the web product.

### Mobile refresh behavior

Dashboard, Clients, History, client profile, and debt profile subscribe to cloud
sync completion and reload SQLite. Pull-to-refresh calls `syncIfConnected()` and
then reloads local data. Screen content remains available when offline.

### Native notifications

Expo Notifications creates Android channels and schedules local reminders and due
alerts. These notifications do not require the Django Web Push subscription. Any
future true server-originated mobile push would require an Expo/FCM/APNs token
registration and delivery pipeline, which does not exist today.

The operating system may delay background jobs and notification delivery based on
battery and permission policies. Test on a release APK, not only Expo Go.

## 12. Local development

### Backend

From PowerShell:

```powershell
cd "D:\ACODIGO\PINGO CORE\Pingo APP\backend"
py -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

Copy `.env.example` to `.env` and adjust local values. Generate VAPID keys once if
testing browser push:

```powershell
.\.venv\Scripts\python manage.py generate_vapid_keys
```

### Web frontend

```powershell
cd "D:\ACODIGO\PINGO CORE\Pingo APP\frontend"
npm ci
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000` when `VITE_API_URL` is absent from
the dev environment. If Vite chooses a different port, add its exact origin to
the backend's CORS/CSRF configuration.

### Mobile

```powershell
cd "D:\ACODIGO\PINGO CORE\Dividas"
npm install
npx expo start
```

Native background tasks, SecureStore, and final keyboard behavior should be tested
in a development build or APK. Expo Go is useful for UI work but is not a complete
production environment.

## 13. Testing and verification

Run these before merging changes that affect a shared contract:

```powershell
cd "D:\ACODIGO\PINGO CORE\Pingo APP\backend"
.\.venv\Scripts\python manage.py test ledger

cd "D:\ACODIGO\PINGO CORE\Pingo APP\frontend"
npm run lint
npm run build

cd "D:\ACODIGO\PINGO CORE\Dividas"
npx tsc --noEmit
npm run lint
```

At the last relevant checks, the backend had 16 passing tests; mobile TypeScript
passed; mobile lint had no errors and pre-existing warnings; frontend build passed
and lint reported warnings in generated/effect code but no blocking errors. Always
use the new command output rather than relying on those historical results.

### Required sync regression scenarios

1. Upgrade a database containing old offline rows; all rows remain visible.
2. Register a new cloud account with phone data and an empty server; phone rows
   appear in cloud and remain on phone.
3. Connect a phone with local data to an account that already has desktop data;
   both sets survive and appear on both sides.
4. Create/edit a client or debt on desktop; foreground mobile pulls it within the
   next successful 30-second cycle or immediately on refresh.
5. Create/pay/edit/delete on mobile; desktop shows it after refresh.
6. Delete a synchronized row offline, reconnect, and confirm the tombstone deletes
   it on the server without resurrection.
7. Retry the same sync batch; no duplicate records appear.
8. Reuse a batch ID with different data; server rejects it.
9. Attempt to sync another user's UUID or device ID; server rejects it and reveals
   no data.
10. Fail the network during pull and upload; all local records and pending deletes
    remain intact.
11. Concurrently edit the same row on desktop and mobile; verify and document the
    expected last-write-wins result.
12. Verify totals after every multi, single-interest, principal, settle, and revert
    operation on both platforms.

### Required account-isolation scenarios

- User A cannot retrieve User B's client integer ID.
- User A cannot retrieve or mutate User B's debt reference.
- User A cannot record/reverse payments against User B's debt.
- User A's bootstrap, dashboard, payments, CSV export, and clear-history operation
  include only User A.
- A mobile token sees only its owner and cannot claim another account's device.
- A guessed public UUID never crosses the account boundary.
- Push subscriptions and event delivery remain owner-scoped.

## 14. Production deployment

### Current topology

```text
Web:      https://pingo.rakei.co.za             Vercel
API:      https://pingo-backend.rakei.co.za     Cloudflare Tunnel -> VPS
VPS path: /home/ubuntu/pingo-backend
Process:  Gunicorn on 127.0.0.1:8001
Database: /home/ubuntu/pingo-backend/db.sqlite3
```

Port 8000 on the VPS was observed to belong to an unrelated Uvicorn process bound
to `0.0.0.0:8000`. Pingo should use `127.0.0.1:8001` unless a fresh port inspection
shows otherwise. PM2 showing "online" does not prove Gunicorn successfully owns its
port; inspect logs and sockets.

The desired checkout layout has `manage.py` directly at:

```text
/home/ubuntu/pingo-backend/manage.py
```

Do not leave a second nested clone such as
`/home/ubuntu/pingo-backend/Pingo-backend/manage.py`.

### First backend deployment

```bash
cd /home/ubuntu
git clone https://github.com/I-Rakei/backend.git pingo-backend
cd /home/ubuntu/pingo-backend

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt gunicorn
.venv/bin/python manage.py migrate
.venv/bin/python manage.py generate_vapid_keys

pm2 start /home/ubuntu/pingo-backend/.venv/bin/gunicorn \
  --name pingo-api \
  --interpreter none \
  --cwd /home/ubuntu/pingo-backend -- \
  config.wsgi:application --bind 127.0.0.1:8001

pm2 start /home/ubuntu/pingo-backend/.venv/bin/python \
  --name pingo-notifications \
  --interpreter none \
  --cwd /home/ubuntu/pingo-backend -- \
  manage.py run_notification_worker

pm2 save
pm2 startup
pm2 status
```

Run the command printed by `pm2 startup` once with sudo, then save again.

`gunicorn` is currently installed explicitly because it is not listed in
`requirements.txt`. Adding it to production requirements would make deployments
more reproducible.

### Updating backend production

Back up first, then pull and migrate:

```bash
cd /home/ubuntu/pingo-backend
mkdir -p backups
sqlite3 db.sqlite3 ".backup 'backups/db-before-deploy.sqlite3'"

git pull
.venv/bin/pip install -r requirements.txt gunicorn
.venv/bin/python manage.py migrate
pm2 restart pingo-api
pm2 restart pingo-notifications
pm2 save
```

Use timestamped backup names in real operation. Never overwrite the only backup.
For meaningful traffic, test restores and copy backups off the VPS.

### Process and port verification

```bash
pm2 status
pm2 logs pingo-api --lines 100
pm2 logs pingo-notifications --lines 100
sudo ss -ltnp | grep ':8001'
curl -i http://127.0.0.1:8001/api/push/config/
```

The last endpoint is public and is a useful process/HTTP smoke test. Authenticated
routes returning 401/403 without credentials is expected.

### Cloudflare Tunnel

The tunnel already runs under PM2 as `pingo-tunnel`. Do not launch a duplicate.
In Cloudflare Zero Trust, the public hostname should map:

```text
pingo-backend.rakei.co.za -> http://localhost:8001
```

A Cloudflare tunnel token was previously pasted into chat. It is intentionally not
recorded here. Treat it as exposed and rotate it in Cloudflare Zero Trust. Store
the replacement only in secure VPS/process configuration, never in Git or docs.

### Vercel web deployment

Connect the frontend repository to Vercel with:

```text
Framework: Vite
Build command: npm run build
Output directory: dist
Environment: VITE_API_URL=https://pingo-backend.rakei.co.za
Domain: pingo.rakei.co.za
```

The tracked `.env.production` contains only the public API base URL. Backend CORS
must include exactly `https://pingo.rakei.co.za`.

### Android APK build

EAS profiles already set the public API endpoint for development, preview, and
production. A preview APK build is:

```powershell
cd "D:\ACODIGO\PINGO CORE\Dividas"
npm install
npx eas-cli@latest login
npx eas-cli@latest build --platform android --profile preview
```

Use `npx eas-cli@latest`; `eas` may not be globally installed and `npx eas` does
not reliably identify the package. A new APK is required for native config, schema,
background scheduling, or bundled JavaScript changes used outside Expo updates.

## 15. Push notification operations

Generate a VAPID key once per environment and keep the private key stable. Changing
it invalidates the relationship with existing browser subscriptions.

`run_notification_worker` is the long-running PM2 process. The one-shot command
`send_due_notifications` is useful for debugging scheduled events. `PushDelivery`
prevents duplicate event messages for the same owner/event key.

Push troubleshooting order:

1. Confirm HTTPS and browser permission.
2. Confirm `/api/push/config/` exposes a public key.
3. Confirm a subscription row exists for the signed-in user.
4. Run the test push endpoint from Settings.
5. Inspect `pingo-notifications` logs.
6. Confirm the VAPID private path exists and PM2 can read it.
7. Confirm the service worker is registered and has no console errors.

## 16. Security and operational notes

- Keep all user-scoped ORM filters in place. Add isolation tests with every new
  endpoint.
- Do not log passwords, tokens, session cookies, subscription auth keys, VAPID
  private material, or full sync payloads containing personal data.
- Phone, email, address, notes, debt values, and payment history are personal and
  financial data. Minimize exports and control backups accordingly.
- A public Git repository is technically possible only after scanning the working
  tree and Git history for secrets. A private repository with an SSH deploy key is
  safer.
- SQLite is appropriate for this small private deployment, but has limited write
  concurrency. Keep transactions short and back it up regularly.
- `DEBUG` must be false in production and `DJANGO_SECRET_KEY` must not use the
  development fallback.
- Browser sessions depend on correct CORS, trusted origin, cookie, and CSRF setup.
  Do not "fix" a CSRF problem by disabling CSRF middleware.
- Token authentication is long-lived until explicitly revoked. Add revocation,
  rotation, and stronger password policy before broader use.
- The tunnel protects reachability, not application authorization. Keep Django
  auth and owner scoping even if only one person is expected to use it.

## 17. Known limitations and future work

These are known boundaries, not instructions to rewrite the system immediately:

1. Desktop freshness is manual after external changes. Add modest authenticated
   polling or server events if live cross-device desktop updates become necessary.
2. Sync is row-level last-write-wins and vulnerable to phone clock skew. A server
   revision protocol would be more robust for multiple active devices.
3. Bootstrap and table pagination are client-side. Add server pagination when an
   account grows large.
4. Reference generation can race under concurrent writers.
5. SQLite limits backend write concurrency and needs disciplined backups.
6. Password policy and token lifecycle are minimal.
7. No server-originated native mobile push exists; current native reminders are
   local schedules.
8. OS background sync timing is not guaranteed.
9. The web lacks URL-per-page routing and route-level code splitting.
10. Backend documentation outside this file has stale upload-only/hourly sync text.
11. Mobile contains older theme/font infrastructure (including Manrope in drawer
    styling) even though the web design mandate is Inter/dark-only. Do not assume a
    web typography request automatically authorizes a mobile-wide restyle.
12. `ALLOWED_HOSTS=*` reflects a private deployment choice, not a strong default.

## 18. Change checklists

### Adding or changing a model field

1. Update the Django model and create a migration.
2. Update serializers and bootstrap shape if web-visible.
3. Update sync snapshot export, upload validation/upsert, and reconciliation.
4. Add an additive mobile SQLite migration; never reset the phone DB.
5. Update mobile export/import types and mappings.
6. Update forms, profiles, documents, CSV, and formatters where relevant.
7. Add backend account-isolation and sync tests.
8. Test upgrade using a copy of an old mobile database.

### Changing financial behavior

1. Define the expected multi and single loan examples numerically.
2. Implement the server rule in `ledger/services.py` transactionally.
3. Implement the equivalent offline rule in `Dividas/lib/database.ts`.
4. Verify installments, payments, reversals, penalties, collected, outstanding,
   capital remaining, total, due date, and status.
5. Sync both directions and ensure reconciliation does not change a valid result.
6. Update invoice, receipt, profile, dashboard, and audit presentation.

### Adding an endpoint

1. Default to authenticated.
2. Scope every queryset to `request.user`.
3. Use serializers/structured parsing, not ad hoc request string handling.
4. Preserve CSRF for session-authenticated writes.
5. Add success, validation, unauthenticated, and cross-account tests.
6. Add the method in `frontend/src/lib/api.js` if web-visible.

### Changing sync

1. Preserve unsynced local rows on pull.
2. Preserve tombstones until a successful canonical response.
3. Keep server UUID lookups owner-scoped.
4. Keep device IDs account-bound.
5. Maintain batch idempotency.
6. Import in dependency order: clients, debts, installments, payments.
7. Delete in reverse dependency order.
8. Recompute derived totals after relationships and deletions settle.
9. Test failed requests, retries, first migration, and concurrent edits.

### Deploying

1. Check Git status and commit only intended changes.
2. Run backend, frontend, and mobile checks relevant to the change.
3. Back up production SQLite before migrations.
4. Preserve `.env`, `.secrets`, and `db.sqlite3` when replacing a checkout.
5. Pull, install dependencies, migrate, and restart only Pingo PM2 processes.
6. Verify port 8001, local HTTP, public tunnel HTTP, and authenticated login.
7. Confirm frontend CORS/CSRF origin and API environment variable.
8. Build a new APK when native or bundled mobile behavior changed.

## 19. Troubleshooting quick reference

### `git pull` says "not a git repository"

The command is being run from the parent/manual-copy directory or a nested clone
was created. Find the actual repository:

```bash
find /home/ubuntu -maxdepth 3 -name .git -type d
```

Then run `git pull` from the directory containing both `.git` and `manage.py`.

### Port 8000 already in use

```bash
sudo ss -ltnp | grep ':8000'
sudo ss -ltnp | grep ':8001'
```

Uvicorn is an ASGI server commonly used by FastAPI/Starlette and some Django ASGI
deployments. In this VPS it belongs to another project. Do not kill it just to fit
Pingo; bind Pingo Gunicorn to 8001 and point the tunnel there.

### PM2 says online but endpoint fails

```bash
pm2 logs pingo-api --lines 100
pm2 describe pingo-api
sudo ss -ltnp | grep ':8001'
curl -i http://127.0.0.1:8001/api/push/config/
```

Check executable path, working directory, port conflict, missing dependencies,
environment file, migrations, and file permissions.

### Browser login gets CSRF or CORS failure

Confirm:

```text
frontend VITE_API_URL = https://pingo-backend.rakei.co.za
backend CORS_ALLOWED_ORIGINS = https://pingo.rakei.co.za
request credentials = include
write header = X-CSRFToken
```

Inspect the exact browser origin. A preview Vercel URL is a different origin and
must be allowed explicitly if it needs API access.

### Manifest syntax or Vite PWA 404

The manifest must be valid JSON served from `/manifest.webmanifest`. The service
worker is `/pingo-sw.js`. There should be no request for
`/@vite-plugin-pwa/pwa-entry-point-loaded` in this project.

### Base UI menu group context error

`MenuGroupLabel` and other group parts must be descendants of `Menu.Group` or
`Menu.RadioGroup`. Follow the wrappers already used by the corrected shadcn
dropdown primitive.

### Mobile does not see desktop data

1. Confirm the phone is connected to the same email account.
2. Confirm network reachability and API URL.
3. Pull to refresh and inspect last attempt/success in Settings.
4. Inspect Django logs for `/api/mobile/sync/`.
5. Verify the token has not been revoked and the device belongs to that user.
6. Remember that the phone displays SQLite; it reloads after a successful import.

### Desktop does not see mobile data

Confirm the mobile sync POST succeeded, then refresh the browser. The current web
app does not automatically poll for another device's changes.

## 20. Final orientation for the next maintainer

Start with these files in order:

1. `backend/ledger/models.py` for the persisted domain.
2. `backend/ledger/services.py` for the financial rules.
3. `backend/ledger/views.py` and `serializers.py` for the web/API contract.
4. `backend/ledger/mobile_sync.py` for cloud reconciliation.
5. `Dividas/lib/database.ts` for offline behavior and phone schema.
6. `Dividas/lib/cloud-sync.ts` for scheduling and transport.
7. `frontend/src/App.jsx` and `frontend/src/lib/api.js` for web state/auth.
8. The relevant screen component for presentation and interactions.

The central design idea is simple: Django owns a secure, account-scoped cloud
ledger; the web app is a direct authenticated view of it; the phone owns a durable
offline ledger and cautiously reconciles it. Protect those three properties before
optimizing convenience. A feature is not complete if it works on desktop but
breaks offline mobile parity, if it syncs but can erase unsynced rows, or if it
works for one account by weakening ownership checks.

That is the Pingo system as implemented at this audit.
