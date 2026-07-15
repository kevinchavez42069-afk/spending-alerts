# Spending Alerts

Daily spending-cap checker: pulls the current bi-weekly pay period's
transactions via Plaid, matches them to budget categories in
[`categories.json`](categories.json), sends a push alert via
[ntfy.sh](https://ntfy.sh) when a pay-period cap is hit or approached, and
writes [`dashboard/data.json`](dashboard/data.json) for the home-screen
dashboard at [`dashboard/index.html`](dashboard/index.html). Anything that
doesn't match a known merchant keyword shows up as "unmatched" and can be
categorized by hand at [`dashboard/categorize.html`](dashboard/categorize.html)
— swiped/tapped decisions are committed straight back to `categories.json` so
the same merchant auto-categorizes next time.

## Setup

### 1. Plaid account

Sign up for a free Plaid account at
[dashboard.plaid.com/signup](https://dashboard.plaid.com/signup) — choose "I
want to use Plaid's APIs to build something for fun." This gives free access
to real bank data for up to 10 connected accounts, no business registration
required. From the Plaid dashboard, grab your `client_id` and `secret` (Sandbox
first, then Production once you're ready for real transactions).

### 2. Link your bank account

Linking a real account requires running Plaid's Link flow once to get a
permanent `access_token`. The easiest way is Plaid's
[Quickstart sample app](https://github.com/plaid/quickstart) run locally:

```
git clone https://github.com/plaid/quickstart
cd quickstart
# follow the repo's setup instructions, using your PLAID_CLIENT_ID / PLAID_SECRET
```

Run it, open the local UI, and go through the Link flow to connect your bank.
The Quickstart app will print the `access_token` — copy it somewhere safe
(you'll add it as a GitHub secret in the next step, not commit it anywhere).

### 3. GitHub Actions secrets

In this repo, go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `PLAID_CLIENT_ID` | from the Plaid dashboard |
| `PLAID_SECRET` | from the Plaid dashboard |
| `PLAID_ACCESS_TOKEN` | from step 2 (your primary checking account) |
| `PLAID_ENV` | `sandbox` or `production` |
| `NTFY_TOPIC` | your private ntfy.sh topic name |
| `ANTHROPIC_API_KEY` | optional — enables Nancy's daily AI summary (see step 6) |

To link an additional account (e.g. a credit card), repeat step 2's Link flow
for it, then add its `access_token` as a new secret named
`PLAID_ACCESS_TOKEN_<NAME>` (e.g. `PLAID_ACCESS_TOKEN_AMEX`) — the script
merges transactions from every `PLAID_ACCESS_TOKEN*` secret automatically.
You'll also need to add a matching line for it in
`.github/workflows/check-spending.yml`'s `env:` block, since GitHub Actions
can't wildcard-match secrets by prefix.

The workflow at
[`.github/workflows/check-spending.yml`](.github/workflows/check-spending.yml)
runs `check_spending.py` daily (13:00 UTC) using these secrets, and commits the
refreshed `dashboard/data.json` back to the repo. You can also trigger it
manually from the Actions tab (`workflow_dispatch`).

### 4. Dashboard

Enable GitHub Pages (**Settings → Pages → Deploy from branch → main**, folder
`/dashboard`, or `/ (root)` with the dashboard under `/dashboard`) and the
dashboard will be live at `https://<username>.github.io/<repo>/dashboard/`. It
reads `dashboard/data.json` client-side — no build step.

### 5. Categorizing unmatched transactions

`dashboard/categorize.html` lets you swipe/tap through transactions that
didn't match any keyword and file them into a category (or mark them "Not
Spending" for transfers/credit card payments). To let it save those decisions,
it needs a GitHub token that can write to this repo:

1. Go to [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Repository access** → **Only select repositories** → this repo
3. **Permissions → Repository permissions** → **Contents: Read and write**
4. Generate the token and paste it into the page when prompted

The token is stored only in your browser's local storage — never committed,
never sent anywhere but directly to GitHub's API.

### 6. Nancy (AI budget coach)

The dashboard has a chat widget ("N" button, bottom right) backed by a small
Cloudflare Worker at
[nancy-pelosi-bot/](../nancy-pelosi-bot) (separate project, not in this repo)
that calls the Anthropic API. It also powers a daily AI-written summary sent
via ntfy alongside the regular cap alerts.

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
   (Settings → API Keys), with a payment method added under Settings → Billing.
2. Add it as the `ANTHROPIC_API_KEY` GitHub secret (table above) — this powers
   the daily summary.
3. Deploy the Worker (`cd nancy-pelosi-bot && npx wrangler deploy`), then set
   its two secrets:
   - `npx.cmd wrangler secret put ANTHROPIC_API_KEY` (same key as above)
   - `npx.cmd wrangler secret put CHAT_SHARED_SECRET` (any random string —
     it's a basic filter against random bots hitting the endpoint directly,
     not real security, since it's embedded in the public page's JS)
4. Update `NANCY_WORKER_URL` and `NANCY_SHARED_SECRET` in
   `dashboard/index.html` to match your deployed Worker URL and the secret
   you picked, and `ALLOWED_ORIGIN` in `nancy-pelosi-bot/wrangler.toml` to
   your GitHub Pages origin.

## Local testing

```
pip install -r requirements.txt
PLAID_CLIENT_ID=... PLAID_SECRET=... PLAID_ACCESS_TOKEN=... PLAID_ENV=sandbox NTFY_TOPIC=... \
  python check_spending.py
```

## Budget context

- Pay periods are bi-weekly (14 days), anchored to a payday in
  `PAY_PERIOD_ANCHOR` in `check_spending.py` — update that date if your pay
  schedule shifts.
- Variable Needs: Groceries $150/period cap, Gas $60/period cap
- Variable Wants (sprint = $0): Going Out (bars/nightlife/events/rideshare),
  Slop (convenience-store junk food), Dining Out (takeout/delivery/restaurants),
  Misc/Other catch-all
- Gym (membership + supplements): tracked but no cap/alert — informational only
- Excluded from spending entirely: credit card payments, savings transfers,
  Zelle/Venmo/Cash App/Apple Cash sent-money (see `excluded.keywords` in
  `categories.json`)
- Excluded accounts: linked accounts matching `excluded_accounts.keywords` in
  `categories.json` (e.g. a self-directed brokerage account) are dropped
  entirely — no balance, no transactions, no entry in the dashboard's
  Accounts section
- Goals: target checking $1,500, target savings $3,000, compared against live
  balances fetched from Plaid each run
- Debt: Delta SkyMiles Amex, ~$2,429/mo target payoff, live balance from
  Plaid. Blue Cash Preferred paid in full each cycle, live balance shown too.
