# Deployment

Operational notes for running Baby Log. This file is deliberately machine-neutral.
Keep your own host-specific runbook (hostnames, paths, cron IDs, ports) in
`DEPLOYMENT.local.md`, which is gitignored.

## Shape of the thing

- One container, one process: `uvicorn app:app` on port 8000.
- One state file: SQLite at `DB_PATH` (WAL mode). Nothing else to provision —
  no Redis, no migrations, no external services.
- Static assets (icons) ship in the image under `/app/icons`.
- The HTML/CSS/JS is embedded in `app.py`; there is no frontend build step.

## Requirements

- Docker + Compose, or Python 3.10+ and `pip install -r requirements.txt`
- A filesystem with a stable path for `data/` (this is your data)
- Something that keeps the machine awake if you depend on it at 3am

## First deploy

```bash
git clone https://github.com/bentonsmith/babytracker.git baby-log && cd baby-log
docker compose up -d --build
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/     # 200
```

The app creates the schema on first request and seeds `meta` (baby name, birth
date). Set those in the UI under ⚙️.

## Ports and exposure

`docker-compose.yml` publishes `127.0.0.1:3001:8000`. **Do not change the host
side to `0.0.0.0`** — the app has no authentication and the loopback bind is
the security boundary.

Reaching it from a phone, in order of preference:

1. **Tailscale / WireGuard on both devices** — the intended setup. With
   `tailscale serve` you get HTTPS and identity for free:
   ```bash
   tailscale serve --bg --set-path /babylog http://127.0.0.1:3001
   ```
   The serve proxy strips the mount prefix; the frontend derives its API base
   from `location.pathname`, so sub-path mounts work without config.
2. **SSH tunnel** — `ssh -L 3001:127.0.0.1:3001 host` from the client.
3. **Authenticating reverse proxy** — Caddy with `basic_auth`, oauth2-proxy, or
   Cloudflare Access in front of `127.0.0.1:3001`. Only do this if you accept
   that the app itself trusts whoever reaches it.

HTTPS matters for one reason beyond hygiene: service workers and Web Push only
run on secure origins (`localhost` is exempt). Over plain HTTP on a LAN IP the
app still works but will not install as a PWA.

## Updates

```bash
git pull
docker compose up -d --build     # DB survives; schema init is idempotent
```

Editing `app.py` also requires a rebuild — there is no volume mount for code.

## Staging copy

Compose defines a second service behind the `test` profile: same image, port
`127.0.0.1:3013`, data in `./data-test`. Use it for anything that touches
writes, so a half-finished change never meets real data.

```bash
sqlite3 data/baby.db ".backup 'data-test/baby.db'"      # fresh snapshot
docker compose --profile test up -d babytracker-test
```

Compose builds a separate image per service, so rebuild **both** names after
changing `app.py`:

```bash
docker compose build babytracker babytracker-test
```

## Backups

`scripts/backup_db.py` copies via the SQLite backup API (consistent during
writes), names the file `babytracker-YYYYmmdd-HHMM.db`, and prunes to 30.

```cron
0 9 * * * cd /path/to/baby-log && python3 scripts/backup_db.py
```

Silent on success (cron-friendly); exits 1 and prints `backup FAILED: ...` when
the DB is missing or unreadable. `BACKUP_VERBOSE=1` prints a line per run.

Two failure modes worth knowing:

- *attempt to write a readonly database* — the DB is probably root-owned from a
  container run with a different uid. `chown` it to `APP_UID`.
- Empty backup directory after days of cron — the job never ran (machine
  asleep, or cron in a different timezone than you expected).

A local copy is not a backup. Sync the directory off-machine.

## Data model

`events` is the single table. Every row has `id`, `type`, `ts_start`, optional
`ts_end`, and a `details` JSON blob interpreted per type. Timers are rows with
`ts_end IS NULL`; the UI closes them. `meta` holds `baby_name` and `birth_date`.

Windowed reads must gate the "still running" case, or instant events (feeds and
diapers have no `ts_end`) leak into every window:

```sql
(ts_start >= :start AND ts_start < :end)
OR (ts_end IS NULL AND type IN ('sleep','tummy'))
```

`/api/events` returns `has_more` so the UI knows whether to page further back.

### Offline-first writes (build 11)

Every create is applied to the UI immediately and queued in `localStorage`
(`baby_outbox_v1`) before the POST is attempted, so a stalled tunnel never
blocks a tap. The POST carries `client_id` (a UUID the client generates); the
server stores it in `events.client_id` with a partial UNIQUE index and returns
the existing row on a repeat, which makes retries idempotent.

- Queued rows render with a ⏳ marker and no edit/delete buttons until synced.
- Retry backoff: 2s → 4s → 8s … capped at 60s, plus flush on `online`, on
  `visibilitychange`, every 30s while the queue is non-empty, and at the end of
  `loadAll()`.
- A 4xx drops the entry and says why (it can never succeed); a network error
  keeps it queued and shows "Saved on this phone — syncing when the connection
  is back".
- `loadAll(page=0)` merges pending local rows into the server response, so a
  refresh can't make an unsynced tap disappear.
- Stop/delete/edit still call the API directly — they need the real id.

### Weight in pounds + ounces

UI entry is two fields (`lb`, `oz`). Payload:
`{"kind":"weight","unit":"lb","lb":8,"oz":6}`. The server validates oz in
`[0,16)`, stores the exact `lb`/`oz` in `details` and `value` as canonical
decimal pounds (`8.375`) so charts, `/api/report` and CSV need no special
case. Display prefers `details.lb`/`details.oz` (exact round-trip) and falls
back to converting `value`, because older rows only carry `value`
(`8.45` → "8 lb 7.2 oz"). `PATCH` accepts the same shape; the report's growth
card formats weight through `fmtLbOz()`.

## History page: charts, day readout and which reports to show (2026-11-06)

The `Totals` card on `/report` renders up to three daily bar rows — feed
volume, **longest stretch between feeds** and sleep per day — over one shared
x-axis of local calendar days, oldest → today.

- **The series is gap-filled client-side** (`chartSeries()`): one bar slot per
  day even when nothing was logged, so a bar's position always matches its
  date. It starts at the first day that has entries, otherwise an `All` range
  squeezes a short history into a few pixels at the right edge.
- **A metric with nothing logged in the range gets no row at all.** An empty
  flat row reads as "zero", which is wrong; this is also what keeps the sleep
  chart out of the way for a parent who doesn't track sleep.
- **Tap or press-and-slide any bar row** to read that day: the readout under
  the chart shows the date plus feeds / volume / diapers / longest stretch /
  sleep. Pointer capture on the row keeps it following the finger; `.barrow`
  carries `touch-action:pan-y` so vertical page scrolling still works. Bars
  carry `data-date`, and the selection is stored as an ISO date, not an index,
  so it survives a range change.
- **Which reports to show** (⚙️ in the header) toggles each card and each
  chart row. The choice is per device in `localStorage['baby_report_cards_v1']`,
  stored as `{key:false}` for hidden items only, so a report added later
  defaults to visible. Section-level visibility is the `rpt-off` class
  (`display:none !important`) applied at the end of `render()` — the intake and
  growth cards set their own `style.display`, so a class is needed to win.

Per-day `max_feed_gap_seconds` is computed in `report_payload()` and assigned
to the day of the **later** feed (an overnight stretch counts toward the
morning). The chain is seeded with the last feed before the window, otherwise
the first feed in range has no predecessor and the same date reports a
different stretch in a one-day range than inside a longer one.

## Medication and dose reminders (added 2026-11-03)

Doses are ordinary `medication` events:
`{"name":"Tylenol","dose":"2.5 ml","interval_min":360}`. `interval_min` is
optional (1–2880); without it the dose is logged but never reminds.

**Scheduling rule: only the newest dose per medication name counts.** Logging
another dose of the same medication therefore reschedules (or cancels, if the
new dose has no interval) the previous reminder — no cleanup needed. Matching
is case-insensitive on `name`.

`check_dose_reminders()` runs every 30 s in a daemon thread and fires when
`due - 30min <= now <= due + 20min`, where `due = ts_start + interval_min`.
The 20-minute grace exists because the host suspends with the lid; a late
nudge beats silence. Text adapts ("due in 12 minutes" / "due now" / "was due
8 minutes ago"). Each fired reminder is recorded in `med_reminders(event_id)`,
so the tick cannot double-send.

### Web push plumbing

- `push_subs(endpoint PK, p256dh, auth, created_at)` holds one row per device.
- VAPID keys are generated on first use with `cryptography` and stored in
  `meta` (`vapid_private`, `vapid_public`). **Never delete them** — new keys
  silently invalidate every existing subscription. py_vapid only parses PEM
  from a path, so the private key is also written to `<db dir>/.vapid-private.pem`.
- `GET /api/push/key` → public key; `POST /api/push/subscribe` (idempotent,
  UPSERT on endpoint); `POST /api/push/test` → sends one notification to every
  subscription. 404/410 responses prune the dead row automatically.
- **The VAPID `sub` claim must be a real reachable URI.** APNs answers
  `403 {"reason":"BadJwtToken"}` for anything it considers invalid — a
  `mailto:…@something.local` address is enough to trigger it, which looks
  exactly like "push is broken". `VAPID_SUB` defaults to `https://example.com`
  as a placeholder — set it to your own URL (or a `mailto:`) in the compose
  env before relying on reminders. Delivery failures are logged (`push failed: …`) and
  returned by `/api/push/test` as `error`, so the UI can tell "no device
  registered" apart from "the push service refused it".
- iOS: requires iOS 16.4+, the app **installed to the Home Screen** (Safari
  tabs cannot receive push), and the permission granted from a tap — the
  "Turn on dose reminders" button in More does all three steps in one gesture.
  `ensurePush()` re-asserts the subscription on load once permission exists, so
  a second device self-registers after one tap.

The UI lists every medication+dose given in the last 24 hours as a chip; a tap
refills name, dose and interval so the next dose is two taps. Medication is a
top-level tile in "What happened?" (not under More, which now only holds
measurements), and the reminder controls live in Settings (⚙️) — the entry
screen just says reminders are off when they are. The Today card still shows
the soonest upcoming dose.

## Mount prefix and the PWA manifest (important)

Serving this app under a path prefix (e.g. `tailscale serve --set-path /baby`)
is supported, but a relative `href="manifest.webmanifest"` breaks the install
flow: when the page URL has **no trailing slash** (`/baby`), relative URLs
resolve against `/`, so iOS reads the *root* site's manifest — the home-screen
icon and label come from whatever lives at `/`. That is how an install can end
up named after a different app entirely.

The pages therefore emit their PWA `<link>` tags from an inline head script
(`PWA_HEAD` in `app.py`) using absolute paths derived from `location.pathname`,
and normalise `/baby` to `/baby/` with `history.replaceState` (which also gives
the service worker the right scope). Any new page must include that script.

Symptom to remember: *right page, wrong icon and name after Add to Home
Screen* = a relative manifest URL resolving outside the mount.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| 200 on `localhost`, nothing on the phone | Tailscale/WireGuard not up on that device, or the serve route was cleared |
| PWA will not install on iOS | Not opened over HTTPS, or not via Safari |
| Icon/name stale after a change | iOS snapshots branding at install: remove + re-add |
| Logged entry "disappears" after refresh | Two instances pointed at different `DB_PATH`s |
| Timers show absurd durations | Clipboard/host clock skew — check `date` in the container |
| `413`/`500` on export | Range too large; `/api/report` caps at 730 days |

## Notes for contributors

- Keep the loopback bind and the "no auth" assumption explicit — a change that
  widens exposure should be a deliberate, documented decision.
- The service worker caches assets only (icons, manifest). Do not add HTML or
  `/api/*` responses to the cache: showing a stale feed log is worse than being
  offline for a second.
- `BASE` (path prefix) is derived from `location.pathname` on both pages; any
  new page must do the same so it works under a sub-path mount.
