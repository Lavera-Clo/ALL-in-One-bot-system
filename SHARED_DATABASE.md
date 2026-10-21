# One shared database for the website, the Android app and the AI bot

```
Customer ──WhatsApp──► Meta ──(one webhook)──► AI bot server (this project)
                                                   │  saves every message
                                                   │  AI replies when the bot is on
                                                   │  sends replies typed by people
                                                   ▼
                                     ONE PostgreSQL database (e.g. Supabase)
                                     contacts · messages (last 21 days) · orders ·
                                     products · AI training data · daily counts
                                                   ▲
                 ┌─────────────────────────────────┼────────────────────────────┐
           Android app                       Business Sender website      AI bot portal
   (Settings → Shared inbox)            ("Sign in with your AI bot account")
```

## What is kept

| Data | Kept for |
|---|---|
| Messages (customer, bot, replies from phone or website) | Each business's **history period**: 21 days by default. Choices: 7, 14, 21, 30, 60, 90, 180 or 365 days (`PATCH /api/tenants/{id}/inbox/settings`). |
| Contacts: number, WhatsApp name, the name you gave them, notes, first contact, last message, message count | Always |
| Messages per day (in / out) | Always, so activity charts go back further than the messages |
| Orders, products, training documents | Always (not affected by the history period) |

A cleanup runs every 6 hours. It counts old messages into the daily totals, then deletes them.
The Android app keeps its own copy of everything it has already received.

## How the apps stay in sync

* **Website:** reads the server directly and gets live updates over a websocket.
* **Android app:** signs in under *Settings → Shared inbox* (or from the first screen without
  a WhatsApp token). While the app is open it asks every 5 seconds for "everything changed since
  my last sync" (`GET /api/tenants/{id}/sync?since=…`). Replies are sent through the server, which
  saves them first. Each reply carries the phone's own id, so a retry after a dropped connection
  still reaches the customer only once. Photos go straight to Meta from the phone and are then
  recorded on the server (`POST …/inbox/chats/{phone}/record`).
* **Delivery and read ticks** come from Meta's status webhooks and appear on every device.
* **Bot switches:** one for the whole business, and one per chat. They're shared too: switching a chat off on the phone
  switches it off on the website.

## Moving to it (one time)

1. **Create the database.** A Supabase project is the easiest. Copy *Project Settings → Database →
   Connection string → URI*. Use the session pooler (port 5432).
2. **Install the drivers** on the server: `pip install -r requirements.txt` (adds `asyncpg`).
3. **Copy the current data.** Stop the server first, then:
   ```bash
   python migrate_to_postgres.py --from sqlite+aiosqlite:///./whatsapp_bot.db --to "postgresql://...your URI..."
   ```
   The script only reads the SQLite file and refuses a target database that already has data.
4. **Point the server at it:** set `DATABASE_URL=postgresql://...` in `.env`, then start the server.
   Owners sign in with their same passwords.
5. **Bring over the Business Sender website's old messages** (its Supabase `messages` table), if it had any:
   ```bash
   set SUPABASE_URL=https://xxxx.supabase.co
   set SUPABASE_SERVICE_ROLE_KEY=...
   python import_supabase_messages.py --days 21
   ```
   Each WhatsApp number needs a business on the server first. Numbers that don't have one are listed and skipped,
   and running the import again is safe.
6. **Switch Meta's webhook** to this server: Meta Developer Dashboard → WhatsApp → Configuration →
   Callback URL `https://YOUR-SERVER/webhook`, with your `META_VERIFY_TOKEN`. Subscribe to
   `messages`. Delivery and read receipts arrive on the same subscription.
7. **Website:** add it to `CORS_ORIGINS` in `.env` (e.g. `https://your-sender.vercel.app`).
8. **Phones:** update the app, open *Settings → Shared inbox* and sign in with the same account.
   The app uploads its own messages from the last 21 days once, so the website shows whole conversations.

## Running the tests

```bash
python test_sync_and_retention.py          # SQLite
python run_tests_postgres.py               # every suite on a real PostgreSQL (pip install pgserver asyncpg)
python test_migrate_to_postgres.py         # the SQLite → PostgreSQL copy
```
Android: `./gradlew testDebugUnitTest` (CloudSyncTest). `CloudApiLiveTest` runs against a test
server when `CLOUD_E2E_URL`, `CLOUD_E2E_EMAIL` and `CLOUD_E2E_PASSWORD` are set.
