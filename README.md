# Whisky Tracker

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
