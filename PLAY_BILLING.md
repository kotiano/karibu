# Google Play Billing — setup guide

Everything needed to take subscription payments through Google Play, in the
order it has to be done. Roughly 45 minutes, most of it waiting for Google.

**Why this exists:** Play's Payments policy names *"cloud software and services
— data storage, business productivity, financial management"* in the list of
things that **require** Google Play Billing, and §4 forbids in-app buttons that
lead to any other payment method. Karibu POS is squarely in that category, so an
in-app "Subscribe" button charging via Paystack is not compliant. See the
blocker section in `karibu-frontend/PLAYSTORE.md` for the full finding.

Paystack is not removed. It stays for anything sold **outside** the app.

---

## What's already built

| Piece | Where |
|---|---|
| Purchase flow, restore, price display | `karibu-frontend/src/hooks/usePlayBilling.ts` |
| Buy / restore UI | `karibu-frontend/src/screens/BillingScreen.tsx` |
| Server-side verification + acknowledge | `karibu-backend/app/services/google_play.py` |
| Redeem endpoint | `POST /api/billing/google/verify` |
| RTDN receiver | `POST /api/billing/webhook/google/{secret}` |
| Subscription `provider` + token columns | migration `3f8a1c6d9e42` |

The two guarantees worth knowing, both verified against Postgres:

- **The Paystack sweep never touches a Play-billed subscription.** It filters on
  `provider` in the query itself, so a restaurant can't be charged on both rails.
- **A purchase token can't be claimed twice.** `google_purchase_token` is UNIQUE,
  so replaying someone else's receipt is rejected by the database.

---

## 1. Create the subscription product

**Play Console → Monetise → Subscriptions → Create subscription**

- **Product ID:** `karibu_pos_monthly` — **permanent, cannot be changed or
  reused after deletion.** It must match `GOOGLE_PLAY_PRODUCT_ID` on the server
  and `SUBSCRIPTION_SKU` in `usePlayBilling.ts`.
- **Name:** Karibu POS Monthly

Then **Add base plan**:

- **Base plan ID:** `monthly`
- **Billing period:** Monthly, auto-renewing
- **Price:** set for Kenya, then let Google convert for other regions

> **On price:** Google takes 15% of subscription revenue. Break-even against
> what Paystack nets you today (~KSh 485 on a KSh 499 charge) is **KSh 570**. At
> KSh 599 you already net more than today; KSh 699 nets ~KSh 594 but is a 40%
> rise on the customer. Whatever you choose, update `SUBSCRIPTION_PRICE_CENTS`
> so the app's fallback figure doesn't contradict the Play sheet.

**Activate** the base plan. A subscription left in draft returns "item
unavailable" in the app with no useful error.

### Free trial
Your 14-day trial is currently enforced server-side. You can either keep that,
or add an **offer** on the base plan with a 14-day free trial so Google enforces
it. Don't do both — the user would get 28 days.

---

## 2. Service account, so the server can verify purchases

The app reports a purchase; the server must confirm it with Google. That needs
API credentials.

### 2a. Create it in Google Cloud
1. [console.cloud.google.com](https://console.cloud.google.com) → create or pick
   a project
2. **APIs & Services → Library** → enable **Google Play Android Developer API**
3. **IAM & Admin → Service Accounts → Create service account**
   - Name: `karibu-play-billing`
   - Skip the optional role grants — permissions come from Play Console
4. Open it → **Keys → Add key → Create new key → JSON** → download

### 2b. Grant it access in Play Console
1. **Play Console → Setup → API access**
2. Link the Google Cloud project from 2a
3. Find the service account → **Grant access**
4. Permissions: **View financial data** and **Manage orders and subscriptions**
5. **Invite user** / Apply

> Propagation takes a few minutes to a few hours. Until it lands you'll get a
> `401` from Google that looks exactly like a bad key — wait before debugging.

### 2c. Put the key on the server
The JSON must be **one line**. Squash it:

```bash
python3 -c "import json,sys;print(json.dumps(json.load(open('key.json'))))"
```

Render → your service → **Environment**:

```
GOOGLE_PLAY_PACKAGE_NAME       ke.co.karibupos.app
GOOGLE_PLAY_PRODUCT_ID         karibu_pos_monthly
GOOGLE_PLAY_SERVICE_ACCOUNT_JSON   {"type":"service_account",...}
```

Escaped `\n` inside `private_key` is handled automatically — dashboards mangle
real newlines, so the code repairs them.

---

## 3. Real-time Developer Notifications

Without these you only learn about a cancellation or failed renewal the next
time the app happens to call verify. With them, Google tells you immediately.

### 3a. Pub/Sub topic
1. Google Cloud → **Pub/Sub → Topics → Create topic**, id `play-billing-rtdn`
2. Grant Google permission to publish to it: **Topic → Permissions → Add
   principal**
   - Principal: `google-play-developer-notifications@system.gserviceaccount.com`
   - Role: **Pub/Sub Publisher**

Skipping that grant is the single most common reason RTDNs never arrive.

### 3b. Push subscription
**Topic → Create subscription**

- ID: `play-billing-push`
- Delivery type: **Push**
- Endpoint URL — including your secret:

```
https://karibu-0ytq-lau7.onrender.com/api/billing/webhook/google/<GOOGLE_PLAY_RTDN_SECRET>
```

Generate the secret with `openssl rand -hex 24` and set it in Render as
`GOOGLE_PLAY_RTDN_SECRET`.

> Pub/Sub push can't send custom headers or sign its payload, so a secret path
> segment is the only edge check available. That's thin on its own — which is
> why the handler treats the notification purely as a nudge and **re-verifies
> the purchase token against Google's API** before changing anything. A forged
> notification grants nothing.

### 3c. Point Play at the topic
**Play Console → Monetise → Monetisation setup → Real-time developer
notifications**

- Topic name: `projects/<your-project-id>/topics/play-billing-rtdn`
- **Send test notification** → your logs should show
  `Received Google Play test notification — topic is wired up`

---

## 4. Build and test

Play Billing cannot be tested in Expo Go or a debug build — the purchase flow
only works for an app **installed from Play**, signed with the same key.

```bash
cd karibu-frontend
npx eas build --platform android --profile production
```

Upload to **internal testing** and install from the Play link, not the APK.

### Licence testers (test purchases, no real money)
**Play Console → Setup → Licence testing** → add the Google accounts that will
test. Those accounts see "Test card, always approves" in the purchase sheet and
are never charged. Renewals are also accelerated — a monthly subscription renews
every 5 minutes, which is how you test the renewal path without waiting a month.

### What to check
- [ ] Buy → sheet shows your price → subscription flips to active
- [ ] Force-quit mid-purchase, reopen, tap **Restore purchase** → still active
- [ ] Cancel in Play Store → RTDN arrives → subscription reflects it
- [ ] Server logs show `Acknowledged Play purchase …`

That last one matters more than it looks. **Google automatically refunds and
revokes any purchase not acknowledged within three days.** A missing
acknowledge looks perfect in testing and silently refunds every real customer
72 hours later.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Item unavailable" in the sheet | Base plan not activated, or product id mismatch between Play Console, `GOOGLE_PLAY_PRODUCT_ID` and `SUBSCRIPTION_SKU` |
| Sheet never opens | App not installed from Play, or signed with a different key than the uploaded build |
| Verify returns 502 | Service-account access hasn't propagated, or the API isn't enabled in Cloud |
| Verify returns 404 | Token genuinely unknown to Google — usually a test purchase against a different package name |
| RTDNs never arrive | Missing Pub/Sub Publisher grant for `google-play-developer-notifications@system.gserviceaccount.com` |
| RTDN endpoint 404s | `GOOGLE_PLAY_RTDN_SECRET` in Render doesn't match the secret in the push URL |
| Purchases refunded after 3 days | Acknowledge failing — check logs for `Failed to acknowledge Play purchase` |

---

## What is deliberately not done

**Existing Paystack subscribers are not migrated.** They keep
`provider = "paystack"` and continue renewing by M-Pesa. Forcing them onto Play
would mean cancelling a working subscription and asking them to buy again — a
good way to lose customers. New in-app subscriptions use Play; migrate the rest
deliberately, if ever.

**iOS is not wired up.** `usePlayBilling` gates on `Platform.OS === "android"`.
An iOS release would need its own App Store product and StoreKit verification.
