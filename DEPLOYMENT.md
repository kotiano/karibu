# Karibu POS — Production Launch Runbook

This is the full, ordered path to a real launch: backend live, then mobile app,
then M-Pesa go-live (real money) last. Do the phases **in order** — each assumes
the previous one is done. Skip any step you've already completed.

Assumed starting point: you have none of the external accounts yet. Where a step
needs one, it's called out.

---

## Phase 0 — Accounts & things to acquire first

You'll need these. Start the slow ones (Daraja, Play Store) early — they involve
review/approval.

| What | Where | Notes |
|---|---|---|
| A server (VPS) | Any cloud (DigitalOcean, Hetzner, AWS Lightsail) | 2GB RAM min; Ubuntu 22.04. This runs the backend. |
| A domain | Any registrar | e.g. `karibupos.co.ke`. You'll point `api.` at the server. |
| Expo account | expo.dev (free) | For building the Android app via EAS. |
| Google Play Console | play.google.com/console | **$25 one-time.** Has a review process — start early. |
| M-Pesa Daraja PRODUCTION app | developer.safaricom.co.ke | Sandbox is instant; **production ("Go Live") needs approval + a real paybill/till.** This is the long pole. |

---

## Phase 1 — Backend live (server + Postgres + HTTPS)

Goal: `https://api.YOURDOMAIN.co.ke/docs` loads over TLS.

### 1.1 Point DNS at the server
In your registrar's DNS, add an **A record**: `api` → your server's IP.
Wait for it to resolve (`ping api.YOURDOMAIN.co.ke` shows the IP).

### 1.2 Prepare the server
SSH in, then:
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER   # log out/in after this
```
Copy the project up (from your machine):
```bash
scp -r karibu-pos/ user@SERVER_IP:~/    # or: git clone your repo on the server
```

### 1.3 Create the production secrets file
On the server, in `karibu-pos/deploy/`, create a `.env` file. **Generate real
secrets — do not reuse these examples:**
```bash
cd ~/karibu-pos/deploy
cat > .env <<EOF
DB_PASSWORD=$(openssl rand -hex 16)
SECRET_KEY=$(openssl rand -hex 32)
JWT_SECRET_KEY=$(openssl rand -hex 32)
MPESA_CALLBACK_SECRET=$(openssl rand -hex 24)
# M-Pesa stays in sandbox until Phase 4 — leave these as-is for now:
MPESA_ENV=sandbox
EOF
chmod 600 .env
```

### 1.4 Get a TLS certificate
The compose file expects certs in `deploy/certs/`. Easiest path — use Let's
Encrypt via certbot on the host, then mount them:
```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d api.YOURDOMAIN.co.ke
mkdir -p ~/karibu-pos/deploy/certs
sudo cp /etc/letsencrypt/live/api.YOURDOMAIN.co.ke/fullchain.pem ~/karibu-pos/deploy/certs/
sudo cp /etc/letsencrypt/live/api.YOURDOMAIN.co.ke/privkey.pem   ~/karibu-pos/deploy/certs/
sudo chown -R $USER ~/karibu-pos/deploy/certs
```
Then edit `deploy/nginx.conf` and set `server_name` to `api.YOURDOMAIN.co.ke`
(and confirm the cert filenames match `fullchain.pem` / `privkey.pem`).

### 1.5 Launch
```bash
cd ~/karibu-pos/deploy
docker compose -f docker-compose.prod.yml up --build -d
```
This runs Postgres, Redis, the `migrate` step (**Alembic** — creates all tables),
three app replicas, one scheduler, and nginx.

Check it:
```bash
docker compose -f docker-compose.prod.yml ps          # all healthy?
docker compose -f docker-compose.prod.yml logs migrate # "Running upgrade ... initial schema"
curl https://api.YOURDOMAIN.co.ke/health               # should return ok
```
Open `https://api.YOURDOMAIN.co.ke/docs` — the API is live.

### 1.6 Seed the first platform admin
The migrate step creates empty tables (no demo data in prod). Create your admin:
```bash
docker compose -f docker-compose.prod.yml exec app1 \
  python create_admin.py --email you@YOURDOMAIN.co.ke --password 'a-strong-password'
```
Restaurants self-register through the app, so that's the only manual account.

---

## Phase 2 — Database migrations (going forward)

You already have Alembic wired. This is how you evolve the schema **without
losing data** — critical once real restaurants are on it.

When you change a model (add a column, table, etc.):
```bash
# On your machine, against a dev DB:
cd backend
alembic revision --autogenerate -m "add staff accounts"
# REVIEW the generated file in alembic/versions/ — autogenerate isn't perfect.
```
Commit that migration file. On the next deploy, the `migrate` service runs
`alembic upgrade head` automatically before the app starts, applying it to
production data safely.

**Backups (do this before your first real customer):**
```bash
# Nightly pg_dump — add to cron on the server:
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U karibu karibu_pos | gzip > ~/backups/karibu_$(date +%F).sql.gz
```

---

## Phase 3 — Mobile app (EAS Build → Play Store)

Goal: an installable Android app pointed at your live API.

### 3.1 IMPORTANT: upgrade Expo SDK first
You're on **SDK 51**. Upgrade before building for production:
```bash
cd frontend
npx expo install expo@latest
npx expo install --fix        # aligns all deps to the new SDK
npx expo-doctor               # fix anything it flags
```
Test thoroughly in Expo Go / a dev build after upgrading — SDK bumps can break
native modules. This is the most likely source of surprises.

### 3.2 Point the app at production
In `frontend/eas.json`, replace every `https://api.YOURDOMAIN.co.ke/api` with
your real domain (in the `preview` and `production` profiles).

### 3.3 Log in to EAS and build a test APK
```bash
npm install -g eas-cli
eas login                     # your Expo account
eas build:configure           # links the project (first time)
eas build --profile preview --platform android
```
This produces an **APK** you can install directly on a phone to test against the
live backend. Register a restaurant, run an order, confirm it all works end to
end. Do NOT skip this — it's your first real integration test.

### 3.4 Production build for the Play Store
```bash
eas build --profile production --platform android   # produces an .aab
```
Then in Play Console: create the app, fill store listing, upload the `.aab` to
the **internal testing** track first, test, then promote to production. (Review
can take a few days.)

---

## Phase 4 — M-Pesa go-live (REAL MONEY — do this LAST)

Everything above must be working first. This is where mistakes cost real money,
so it's deliberately last and has its own checks.

### 4.1 Get production Daraja credentials
On developer.safaricom.co.ke, take your app through **Go Live**: you'll need a
real paybill or till number and Safaricom's approval. You get:
- Production Consumer Key + Secret
- Production Passkey
- Your Shortcode (paybill/till)

### 4.2 Set the production M-Pesa env
On the server, edit `deploy/.env` — switch from sandbox to production:
```bash
MPESA_ENV=production
MPESA_CONSUMER_KEY=<prod key>
MPESA_CONSUMER_SECRET=<prod secret>
MPESA_SHORTCODE=<your paybill/till>
MPESA_PASSKEY=<prod passkey>
MPESA_CALLBACK_URL=https://api.YOURDOMAIN.co.ke/api/billing/mpesa/callback
# keep the MPESA_CALLBACK_SECRET you generated in Phase 1
```
Register the callback URL with Safaricom (their portal / support). It **must**
be the public HTTPS URL above — Daraja can't reach localhost.

Restart to pick up the new env:
```bash
docker compose -f docker-compose.prod.yml up -d
```

### 4.3 Test with a SMALL real transaction first
Before pointing it at customers: subscribe one test restaurant and pay the
KSh 499 with a real phone. Confirm:
- The STK push arrives on the phone
- Payment completes and the subscription activates
- The callback is received (check `app1` logs) and **not** double-counted
- The billing charge shows once in the admin panel

The billing code has four anti-double-charge safeguards (row locks, a unique
index on `(subscription_id, period_start)`, idempotent callbacks, token
versioning) — but this test is what proves they hold against real Daraja.

### 4.4 Go live
Once the test transaction is clean, you're live. Watch the scheduler logs for the
first real renewal/dunning cycle.

---

## Launch-day checklist (quick reference)

- [ ] DNS `api.` → server, resolving
- [ ] Real secrets in `deploy/.env` (chmod 600), NOT the examples
- [ ] TLS cert in place, `https://api.../docs` loads
- [ ] `migrate` ran Alembic; all containers healthy
- [ ] Platform admin created
- [ ] Nightly pg_dump backup in cron
- [ ] Expo SDK upgraded; `expo-doctor` clean
- [ ] `eas.json` points at prod domain
- [ ] Preview APK tested end-to-end against live API
- [ ] Production `.aab` in Play Console internal track
- [ ] Daraja PRODUCTION creds set; callback URL registered
- [ ] One small REAL M-Pesa transaction verified (no double charge)
- [ ] Then, and only then: open to customers
