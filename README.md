# Whisky Tracker

## Manual collection run

Configure the local `.env` values documented in `.env.example`, then run one complete collection:

```bash
python -m whisky_tracker run
```

Use `--dry-run` for a safe inspection pass. Dry runs still persist observations and evaluate alerts
so history and idempotency behave realistically, but they never send Telegram messages or mark
alerts as sent:

```bash
python -m whisky_tracker run --dry-run
python -m whisky_tracker run --dry-run --retailer coto --retailer jumbo
```

Configure `USER_LATITUDE` and `USER_LONGITUDE` together to resolve the real delivery context for
Coto and Jumbo. `USER_POSTAL_CODE` is optional and is also used by Carrefour; the legacy
`CARREFOUR_POSTAL_CODE` remains a fallback. The normal runner skips Coto and Jumbo when coordinates
are absent rather than mixing generic prices into location-aware history. Their adapters still
support an explicit no-context generic call for isolated developer debugging. Mercado Libre is
optional and is skipped unless its access token is configured.

## GitHub Actions deployment

The `Whisky Tracker` workflow runs at 13:17 and 21:17 UTC, approximately 10:17 and 18:17 in
Argentina. GitHub schedules are approximate and may be delayed under load. The schedule only runs
from the repository's default branch.

Configure these repository **Secrets** under Settings → Secrets and variables → Actions:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- eventually `USER_LATITUDE` and `USER_LONGITUDE` for location-aware cloud runs; the workflow must
  map these Secrets into its environment before cloud resolution is enabled
- optionally `MERCADOLIBRE_ACCESS_TOKEN`, `MERCADOLIBRE_REFRESH_TOKEN`,
  `MERCADOLIBRE_CLIENT_ID`, and `MERCADOLIBRE_CLIENT_SECRET`

Configure these non-secret repository **Variables**:

- `CARREFOUR_POSTAL_CODE` (recommended: `1428` for the intended Market Juramento context)
- optionally `USER_POSTAL_CODE`; unlike exact coordinates, a broad postcode is normally suitable
  for a repository Variable
- `NOTIFICATIONS_ENABLED`: leave unset or set to anything other than `true` while validating;
  set exactly `true` to enable notifications on scheduled runs
- optionally `MAX_NOTIFICATIONS_PER_RUN` and the three `MINIMUM_*_PERCENTAGE` thresholds

From Actions → Whisky Tracker → Run workflow, keep `dry_run` enabled for the first cloud run. This
tests collection, matching, persistence, and alert evaluation without sending or marking alerts.
After reviewing its job summary, a manual run with `dry_run` disabled exercises real delivery.

Production starts with a fresh database. The workflow does not upload the local development
database. Durable state is stored outside the source branch on an orphan `whisky-tracker-state`
branch containing the current and previous validated SQLite generations. A force-with-lease and a
single Actions concurrency group prevent state rollback by overlapping runs. Each successful run
also uploads a 30-day recovery artifact. Do not manually edit the state branch. In a public
repository its database contents are public; they contain price/listing history but should never
contain configured tokens.

Exact coordinates can reveal a home or frequently used delivery location. Store them as GitHub
Actions Secrets even though they are not authentication credentials: Secrets mask accidental log
output and are not readable by ordinary repository viewers, while Variables are intended for
non-sensitive configuration. Repository administrators and workflows permitted to use the Secrets
still have access while the workflow runs. Coordinates are used only for retailer resolution;
resolved observations retain seller/branch/region context without coordinates, and persistence
rejects any observation that still contains transient coordinates. Public state snapshots therefore
contain derived commercial context rather than the exact location input.

Every restore and publication runs SQLite integrity validation. Once the state branch exists, a
restore/network failure stops the job rather than silently initializing an empty database. Run
diagnostics and the application summary appear on the workflow run's Summary page.

Mercado Libre token refresh is currently in-memory only. GitHub Actions cannot safely rewrite an
Actions Secret using its normal workflow token, so a refreshed access/refresh token is not retained
between runs. Leave Mercado Libre unconfigured until credentials with a suitable lifetime or a
separate approved secret-rotation mechanism are available.

To disable autonomous execution without editing application logic, disable the `Whisky Tracker`
workflow from its Actions page. Setting `NOTIFICATIONS_ENABLED` to `false` keeps scheduled
collection/state updates active but makes scheduled executions dry runs.

## Mercado Libre development authentication

Mercado Libre retrieval uses its official authenticated API. Create a Mercado Libre developer
application and complete its OAuth authorization flow to obtain an access token; credentials must
remain local and must never be committed.

Copy the placeholders from `.env.example` into your local `.env` and configure
`MERCADOLIBRE_ACCESS_TOKEN`. To enable automatic refresh, also configure
`MERCADOLIBRE_REFRESH_TOKEN`, `MERCADOLIBRE_CLIENT_ID`, and `MERCADOLIBRE_CLIENT_SECRET`.
Environment loading belongs in the application entry point. Construct `MercadoLibreAuth` from that
configuration and inject it into `MercadoLibreAdapter`; the adapter does not read process environment
variables itself.

Once a valid token is available, a developer can run a small async smoke script that constructs the
authentication object and calls `await adapter.search_products("whisky")`. Do not place token values
in scripts, tests, command history, or repository files.

## Telegram local setup

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` only in the ignored local `.env`. The application
entry point should construct `TelegramConfig` from those values and inject it into
`TelegramNotifier`; the notifier does not load environment variables.

To discover a private chat ID, first send the bot a message and then call the notifier's
`get_updates()` method. Inspect only each update's `message.chat.id` (or
`channel_post.chat.id` for a channel), select the chat you intentionally messaged, and store that
numeric value as `TELEGRAM_CHAT_ID`. Do not save the remaining update payload, print the bot token,
or place a token-bearing Bot API URL in logs. If a webhook is configured, `getUpdates` is unavailable
until the webhook is removed.
