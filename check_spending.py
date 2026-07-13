"""
Weekly spending alert system.

Pulls the last 7 days of transactions from Plaid, checks them against the
category caps in categories.json, sends a push notification via ntfy.sh
when you're approaching or over a cap, and writes dashboard/data.json so
the home-screen dashboard can display current progress.

Environment variables required (set as GitHub Actions secrets):
  PLAID_CLIENT_ID
  PLAID_SECRET
  PLAID_ACCESS_TOKEN   (obtained once via the Plaid Link flow - see README)
  PLAID_ENV            ("production" once you're out of sandbox)
  NTFY_TOPIC           (your private ntfy.sh topic name)
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
PLAID_ACCESS_TOKEN = os.environ["PLAID_ACCESS_TOKEN"]
PLAID_ENV = os.environ.get("PLAID_ENV", "production")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

with open(os.path.join(os.path.dirname(__file__), "categories.json")) as f:
    CATEGORIES = json.load(f)


# ---------- Plaid setup ----------

def get_plaid_client():
    configuration = Configuration(
        host=PLAID_HOSTS[PLAID_ENV],
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    api_client = ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def get_last_7_days_transactions():
    client = get_plaid_client()
    end_date = date.today()
    start_date = end_date - timedelta(days=7)

    request = TransactionsGetRequest(
        access_token=PLAID_ACCESS_TOKEN,
        start_date=start_date,
        end_date=end_date,
    )
    response = client.transactions_get(request)
    transactions = response["transactions"]

    # Handle pagination if there are many transactions
    while len(transactions) < response["total_transactions"]:
        request = TransactionsGetRequest(
            access_token=PLAID_ACCESS_TOKEN,
            start_date=start_date,
            end_date=end_date,
            options={"offset": len(transactions)},
        )
        response = client.transactions_get(request)
        transactions.extend(response["transactions"])

    return transactions


# ---------- Categorization ----------

def categorize(transactions):
    """Bucket transactions into our categories by merchant name keyword match."""
    totals = {
        "variable_needs": {cat: 0.0 for cat in CATEGORIES["variable_needs"]},
        "variable_wants": {cat: 0.0 for cat in CATEGORIES["variable_wants"]},
    }
    unmatched = []

    for txn in transactions:
        amount = txn["amount"]  # Plaid: positive = money out
        if amount <= 0:
            continue  # skip refunds/credits
        name = (txn["name"] or "").upper()

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
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority},
    )


def check_caps_and_alert(totals):
    alerts_sent = []
    for group in ("variable_needs", "variable_wants"):
        for cat, cfg in CATEGORIES[group].items():
            cap = cfg["weekly_cap"]
            spent = totals[group][cat]
            label = cat.replace("_", " ").title()

            if cap == 0 and spent > 0:
                msg = f"{label}: ${spent:.2f} spent this week (target: $0 during sprint)"
                send_ntfy(msg, title="⚠️ Sprint category spend", priority="high")
                alerts_sent.append(msg)
            elif cap > 0:
                pct = spent / cap
                if pct >= 1.0:
                    msg = f"{label}: ${spent:.2f} of ${cap:.2f} weekly cap — OVER by ${spent - cap:.2f}"
                    send_ntfy(msg, title="🚨 Over weekly cap", priority="high")
                    alerts_sent.append(msg)
                elif pct >= 0.8:
                    msg = f"{label}: ${spent:.2f} of ${cap:.2f} weekly cap ({pct*100:.0f}%)"
                    send_ntfy(msg, title="⚠️ Approaching weekly cap", priority="default")
                    alerts_sent.append(msg)
    return alerts_sent


# ---------- Dashboard data ----------

def write_dashboard_data(totals, unmatched):
    output = {
        "last_updated": date.today().isoformat(),
        "categories": totals,
        "unmatched_transactions": unmatched,
        "caps": {
            group: {cat: cfg["weekly_cap"] for cat, cfg in CATEGORIES[group].items()}
            for group in ("variable_needs", "variable_wants")
        },
        "debt": CATEGORIES["debt"],
    }
    dashboard_path = os.path.join(
        os.path.dirname(__file__), "dashboard", "data.json"
    )
    with open(dashboard_path, "w") as f:
        json.dump(output, f, indent=2)


# ---------- Main ----------

def main():
    transactions = get_last_7_days_transactions()
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
