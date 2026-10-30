# Hosting: database, bot server + webhook, website

```
                 Meta (WhatsApp) ──webhook──┐
                                            ▼
   Business Sender website  ──API──►  Bot server  ◄──API──  Android app
   (Vercel, free)                     (small VM, always on)   (phones)
                                            │
                                            ▼
                                   Database (Supabase, free)
```

| Part | Where | Cost |
|---|---|---|
| Database (PostgreSQL) | **Supabase** | Free tier (500 MB) |
| Bot server = webhook + API + portal + AI | **A small always-on VM**: Oracle Cloud Always Free, Google Cloud e2-micro (free), or a $4-6/month VPS | Free – $6 |
| Business Sender website | **Vercel** (already set up) | Free |
| Domain for the bot server | Any registrar (~$10/year), or a free subdomain from duckdns.org | Free – $10/yr |

The bot server can't go on Vercel or Cloudflare: it must stay running (live inbox websockets,
background jobs like history cleanup and cart reminders, file uploads).

## 1. Database: Supabase (10 minutes)

1. supabase.com → New project. Choose the region closest to the server (e.g. Mumbai/Singapore
   for Sri Lanka). Save the database password.
2. Project Settings → Database → **Connection string → URI**, *Session pooler* (port 5432).
   It looks like `postgresql://postgres.abcd:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
3. Tables are created by the server on first start. To bring your current data along, run
   the data across once (see SHARED_DATABASE.md; the one-time copy tools were removed in the October 2026 clean-up - restore them from git history if needed). This works from your own PC.

Free projects can pause after a week with no activity; the server's regular work normally keeps
it active. Check the Supabase dashboard if the bot ever stops answering.

## 2. Bot server (30 minutes)

1. **Create the VM**: Ubuntu 22.04 or 24.04, 1 GB RAM or more. Allow incoming ports 80 and 443
   in the cloud firewall (Oracle: VCN → Security List → Ingress rules; Google: "Allow HTTP/HTTPS").
2. **Domain**: create an `A` record, e.g. `bot.yourshop.com` → the VM's public IP.
3. **Push this project to GitHub** (`git push`). The server downloads the code from there.
   The repository must not contain `.env` (it doesn't: `.env` is git-ignored).
4. **On the VM** (SSH in):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Lavera-Clo/ALL-in-One-bot-system/main/deploy/setup_server.sh -o setup.sh
   sudo bash setup.sh bot.yourshop.com
   ```
   For a private repository, clone it first with a GitHub token, or set `REPO=` to the address.
5. **Settings**: `sudo nano /opt/whatsapp-bot/.env`:
   * `DATABASE_URL=` the Supabase URI from step 1
   * `META_VERIFY_TOKEN=` any long random text (you'll type the same into Meta)
   * `META_APP_SECRET=` Meta → App settings → Basic → App secret (**required**: blocks fake webhook posts)
   * `ADMIN_USERNAME` / `ADMIN_PASSWORD` for the first owner account
   * AI keys: `GROQ_API_KEY`, `GEMINI_API_KEY`, ...
   * `CORS_ORIGINS=https://your-website.vercel.app`
   * `BASE_URL=https://bot.yourshop.com` (set by the script)
6. Start it and check:
   ```bash
   sudo systemctl start whatsapp-bot
   curl https://bot.yourshop.com/health          # {"status":"healthy","database":"connected"}
   sudo journalctl -u whatsapp-bot -f            # live logs
   ```
   The portal is at `https://bot.yourshop.com/`.

**Updating later**: `git push` from your PC, then on the VM `sudo bash setup.sh bot.yourshop.com`
again. It pulls the new code, installs packages and restarts.

**Backups**: Supabase backs up the database. Uploaded files (receipts, training documents) are on
the VM in `/opt/whatsapp-bot/uploads` and `static/tenant_docs`, so copy them off now and then:
`sudo tar czf ~/uploads-$(date +%F).tgz -C /opt/whatsapp-bot uploads static/tenant_docs`.

## 3. Webhook: point Meta at the server (5 minutes)

Meta Developer Dashboard → your app → WhatsApp → **Configuration** → Webhook → Edit:
* Callback URL: `https://bot.yourshop.com/webhook`
* Verify token: the same text as `META_VERIFY_TOKEN`
* Verify and save, then subscribe to the **messages** field.

Send a WhatsApp message to the business number: it should appear in the portal's inbox and the
logs. One Meta app has one webhook, so this replaces the website's old `/api/webhook`.

For a permanent setup, use a **System User access token** (Business Settings → System users),
not the 24-hour temporary token from the API Setup page. Paste it per business in the portal
(Connect WhatsApp).

## 4. Website: Vercel (5 minutes)

Vercel → the `vercel-webhook` project → Settings → Environment Variables:
* `NEXT_PUBLIC_BOT_API_URL=https://bot.yourshop.com` (so sign-in doesn't ask for the address)
* Keep `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_ANON_KEY` if phones still link by QR code.

Redeploy (push to GitHub, or Deployments → Redeploy). Then open the site → *Sign in with your AI
bot account*.

## 5. Phones

Install the updated app → Settings → **Shared inbox** → server address `bot.yourshop.com`, with
the same email and password as the portal.

## Checklist

- [ ] `curl https://bot.yourshop.com/health` → healthy
- [ ] Meta webhook verified; a test WhatsApp message shows in the portal inbox
- [ ] `META_APP_SECRET` set (log has no "signatures are NOT checked" warning)
- [ ] Website signs in, shows chats, can reply
- [ ] Phone app signs in and syncs
- [ ] Default `ADMIN_PASSWORD` changed
