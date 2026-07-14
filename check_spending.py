"""
Bi-weekly spending alert system.

Pulls the current pay period's transactions from Plaid, checks them against
the category caps in categories.json, sends a push notification via ntfy.sh
when you're approaching or over a cap, and writes dashboard/data.json so
the home-screen dashboard can display current progress.

Environment variables required (set as GitHub Actions secrets):
  PLAID_CLIENT_ID
  PLAID_SECRET
  PLAID_ACCESS_TOKEN          (obtained once via the Plaid Link flow - see README)
  PLAID_ACCESS_TOKEN_<NAME>   (optional - one per additional linked account,
                                e.g. PLAID_ACCESS_TOKEN_AMEX; transactions
                                from every PLAID_ACCESS_TOKEN* secret are
                                pulled and merged)
  PLAID_ENV                   ("production" once you're out of sandbox)
  NTFY_TOPIC                  (your private ntfy.sh topic name)
"""

import json
import os
from datetime import date, timedelta

import requests
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.configuration import Configuration
from plaid.api_client import ApiClient

# ---------- Config ----------

PLAID_CLIENT_ID = os.environ["PLAID_CLIENT_ID"]
PLAID_SECRET = os.environ["PLAID_SECRET"]
PLAID_ENV = os.environ.get("PLAID_ENV", "production")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# Every PLAID_ACCESS_TOKEN* env var is a separate linked account (checking,
# credit card, etc.) - transactions from all of them are pulled and merged.
PLAID_ACCESS_TOKENS = {
    key: value
    for key, value in sorted(os.environ.items())
    if key.startswith("PLAID_ACCESS_TOKEN") and value
}

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# Pay periods are 14-day blocks starting from this anchor date (a payday).
PAY_PERIOD_ANCHOR = date(2026, 7, 8)

with open(os.path.join(os.path.dirname(__file__), "categories.json")) as f:
    CATEGORIES = json.load(f)


def current_pay_period():
    today = date.today()
    period_index = (today - PAY_PERIOD_ANCHOR).days // 14
    start_date = PAY_PERIOD_ANCHOR + timedelta(days=period_index * 14)
    return start_date, today


# ---------- Plaid setup ----------

def get_plaid_client():
    configuration = Configuration(
        host=PLAID_HOSTS[PLAID_ENV],
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    api_client = ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def get_account_transactions(client, access_token, start_date, end_date):
    request = TransactionsGetRequest(
        access_token=access_token,
        start_date=start_date,
        end_date=end_date,
    )
    response = client.transactions_get(request)
    transactions = response["transactions"]

    # Handle pagination if there are many transactions
    while len(transactions) < response["total_transactions"]:
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options={"offset": len(transactions)},
        )
        response = client.transactions_get(request)
        transactions.extend(response["transactions"])

    return transactions


def get_pay_period_transactions():
    client = get_plaid_client()
    start_date, end_date = current_pay_period()

    transactions = []
    for access_token in PLAID_ACCESS_TOKENS.values():
        transactions.extend(
            get_account_transactions(client, access_token, start_date, end_date)
        )
    return transactions


# ---------- Categorization ----------

def categorize(transactions):
    """Bucket transactions into our categories by merchant name keyword match."""
    totals = {
        "variable_needs": {cat: 0.0 for cat in CATEGORIES["variable_needs"]},
        "variable_wants": {cat: 0.0 for cat in CATEGORIES["variable_wants"]},
    }
    excluded_keywords = [kw.upper() for kw in CATEGORIES.get("excluded", {}).get("keywords", [])]
    unmatched = []

    for txn in transactions:
        amount = txn["amount"]  # Plaid: positive = money out
        if amount <= 0:
            continue  # skip refunds/credits
        name = (txn["name"] or "").upper()

        if any(kw in name for kw in excluded_keywords):
            continue  # transfer/payment/credit card bill, not discretionary spending

        matched = False
        for group in ("variable_needs", "variable_wants"):
            for cat, cfg in CATEGORIES[group].items():
                if any(kw.upper() in name for kw in cfg["keywords"]):
                    totals[group][cat] += amount
                    matched = True
                    break
            if matched:
                break

        if not matched:
            # Falls into misc/other "bullshit" catch-all
            totals["variable_wants"]["misc_other"] += amount
            unmatched.append({"name": txn["name"], "amount": amount})

    return totals, unmatched


# ---------- Alerts ----------

def send_ntfy(message, title="Spending Alert", priority="default"):
    # ntfy expects non-ASCII header values (e.g. emoji in the title) UTF-8
    # encoded as bytes; HTTP headers are otherwise restricted to Latin-1.
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": priority},
    )


def check_caps_and_alert(totals):
    alerts_sent = []
    for group in ("variable_needs", "variable_wants"):
        for cat, cfg in CATEGORIES[group].items():
            cap = cfg["biweekly_cap"]
            if cap is None:
                continue  # informational-only category (e.g. gym), no alerting
            spent = totals[group][cat]
            label = cat.replace("_", " ").title()

            if cap == 0 and spent > 0:
                msg = f"{label}: ${spent:.2f} spent this pay period (target: $0 during sprint)"
                send_ntfy(msg, title="⚠️ Sprint category spend", priority="high")
                alerts_sent.append(msg)
            elif cap > 0:
                pct = spent / cap
                if pct >= 1.0:
                    msg = f"{label}: ${spent:.2f} of ${cap:.2f} pay period cap — OVER by ${spent - cap:.2f}"
                    send_ntfy(msg, title="🚨 Over pay period cap", priority="high")
                    alerts_sent.append(msg)
                elif pct >= 0.8:
                    msg = f"{label}: ${spent:.2f} of ${cap:.2f} pay period cap ({pct*100:.0f}%)"
                    send_ntfy(msg, title="⚠️ Approaching pay period cap", priority="default")
                    alerts_sent.append(msg)
    return alerts_sent


# ---------- Dashboard data ----------

def write_dashboard_data(totals, unmatched):
    period_start, period_end = current_pay_period()
    output = {
        "last_updated": date.today().isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "categories": totals,
        "unmatched_transactions": unmatched,
        "caps": {
            group: {cat: cfg["biweekly_cap"] for cat, cfg in CATEGORIES[group].items()}
            for group in ("variable_needs", "variable_wants")
        },
        "goals": CATEGORIES["goals"],
        "debt": CATEGORIES["debt"],
    }
    dashboard_path = os.path.join(
        os.path.dirname(__file__), "dashboard", "data.json"
    )
    with open(dashboard_path, "w") as f:
        json.dump(output, f, indent=2)


# ---------- Main ----------

def main():
    transactions = get_pay_period_transactions()
    totals, unmatched = categorize(transactions)
    alerts = check_caps_and_alert(totals)
    write_dashboard_data(totals, unmatched)

    print(f"Checked {len(transactions)} transactions.")
    print(f"Totals: {json.dumps(totals, indent=2)}")
    if alerts:
        print(f"Sent {len(alerts)} alert(s).")
    else:
        print("No alerts needed - all categories within range.")


if __name__ == "__main__":
    main()
