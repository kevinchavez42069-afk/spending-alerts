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
  ANTHROPIC_API_KEY           (optional - enables Nancy's AI summary, sent
                                every other day; skipped entirely without it)
"""

import json
import os
from datetime import date, timedelta

import requests
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.configuration import Configuration
from plaid.api_client import ApiClient

# ---------- Config ----------

PLAID_CLIENT_ID = os.environ["PLAID_CLIENT_ID"]
PLAID_SECRET = os.environ["PLAID_SECRET"]
PLAID_ENV = os.environ.get("PLAID_ENV", "production")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

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

# Nancy's summary only fires every other day (not every run) to cut down on
# notification noise - gated the same way pay periods are, off a fixed
# anchor, so it doesn't need any stored "last sent" state.
NANCY_SUMMARY_INTERVAL_DAYS = 2
NANCY_SUMMARY_ANCHOR = date(2026, 7, 8)

DASHBOARD_DATA_PATH = os.path.join(os.path.dirname(__file__), "dashboard", "data.json")

with open(os.path.join(os.path.dirname(__file__), "categories.json")) as f:
    CATEGORIES = json.load(f)


def current_pay_period():
    today = date.today()
    period_index = (today - PAY_PERIOD_ANCHOR).days // 14
    start_date = PAY_PERIOD_ANCHOR + timedelta(days=period_index * 14)
    return start_date, today


def should_send_nancy_summary():
    return (date.today() - NANCY_SUMMARY_ANCHOR).days % NANCY_SUMMARY_INTERVAL_DAYS == 0


def load_previous_nancy_messages():
    if not os.path.exists(DASHBOARD_DATA_PATH):
        return []
    with open(DASHBOARD_DATA_PATH) as f:
        try:
            return json.load(f).get("nancy_messages", [])
        except json.JSONDecodeError:
            return []


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


def get_all_accounts():
    client = get_plaid_client()
    accounts = []
    for access_token in PLAID_ACCESS_TOKENS.values():
        request = AccountsBalanceGetRequest(access_token=access_token)
        response = client.accounts_balance_get(request)
        accounts.extend(response["accounts"])
    return accounts


def is_excluded_account(acct):
    """True for accounts matching excluded_accounts.keywords in categories.json
    (e.g. a self-directed brokerage account) - these are dropped entirely
    from balances, the accounts list, and transaction categorization,
    since they're not spending accounts this tool should track."""
    keywords = [kw.upper() for kw in CATEGORIES.get("excluded_accounts", {}).get("keywords", [])]
    if not keywords:
        return False
    label = f"{acct['name'] or ''} {acct['official_name'] or ''}".upper()
    return any(kw in label for kw in keywords)


def build_account_labels(accounts):
    """Map account_id -> friendly display name, current balance, and
    type/subtype/credit-limit metadata, so transactions (which only carry
    an account_id) can be grouped/labeled and the dashboard can render
    type-aware balance coloring (checking/savings targets, credit
    utilization)."""
    labels = {}
    balances_by_id = {}
    meta_by_id = {}
    for acct in accounts:
        labels[acct["account_id"]] = acct["name"] or acct["official_name"] or "Account"
        balances_by_id[acct["account_id"]] = acct["balances"]["current"]
        # Plaid's type/subtype fields are enums, not plain strings - str()
        # them so the dashboard gets a plain JSON string to compare against.
        meta_by_id[acct["account_id"]] = {
            "type": str(acct["type"]),
            "subtype": str(acct["subtype"]),
            "limit": acct["balances"]["limit"],
        }
    return labels, balances_by_id, meta_by_id


def summarize_balances(accounts):
    """Checking/savings are picked out by subtype; credit accounts are
    matched to a debt entry in categories.json by account_keywords against
    the account's name, since more than one credit card can be linked (e.g.
    two Amex cards under one Item)."""
    # Plaid's subtype/type fields are enums, not plain strings, so they must
    # be str()-cast before comparing to a literal - direct == against a
    # string is always False even though they print identically.
    checking = next((a for a in accounts if str(a["subtype"]) == "checking"), None)
    savings = next((a for a in accounts if str(a["subtype"]) == "savings"), None)
    credit_accounts = [a for a in accounts if str(a["type"]) == "credit"]

    credit_balances = {}
    for debt_key, debt_cfg in CATEGORIES.get("debt", {}).items():
        keywords = [kw.upper() for kw in debt_cfg.get("account_keywords", [])]
        if not keywords:
            continue
        for acct in credit_accounts:
            label = f"{acct['name'] or ''} {acct['official_name'] or ''}".upper()
            if any(kw in label for kw in keywords):
                credit_balances[debt_key] = acct["balances"]["current"]
                break

    return {
        "checking_balance": checking["balances"]["current"] if checking else None,
        "savings_balance": savings["balances"]["current"] if savings else None,
        "credit_balances": credit_balances,
    }


# ---------- Categorization ----------

def categorize(transactions, account_labels):
    """Bucket transactions into our categories by merchant name keyword match.

    `overrides` in categories.json (exact uppercased transaction name ->
    "group.category", or "excluded") takes precedence over keyword matching
    entirely, so a single mis-categorized merchant can be corrected without
    disturbing other transactions that share a broad keyword (e.g. "TST*").

    Returns (totals, detail, unmatched, by_account):
      totals       - {group: {cat: summed_amount}} (unchanged shape from before)
      detail       - {group: {cat: [{name, amount, account}, ...]}} - the
                      transactions behind each total, for the dashboard's
                      per-category drill-down
      unmatched    - transactions that fell through to misc_other
      by_account   - {account_label: [{name, amount, category}, ...]} - every
                      included transaction grouped by linked account, for the
                      dashboard's per-account view
    """
    totals = {
        "variable_needs": {cat: 0.0 for cat in CATEGORIES["variable_needs"]},
        "variable_wants": {cat: 0.0 for cat in CATEGORIES["variable_wants"]},
    }
    detail = {
        "variable_needs": {cat: [] for cat in CATEGORIES["variable_needs"]},
        "variable_wants": {cat: [] for cat in CATEGORIES["variable_wants"]},
    }
    excluded_keywords = [kw.upper() for kw in CATEGORIES.get("excluded", {}).get("keywords", [])]
    overrides = {k.upper(): v for k, v in CATEGORIES.get("overrides", {}).items()}
    unmatched = []
    by_account = {}

    for txn in transactions:
        amount = txn["amount"]  # Plaid: positive = money out
        if amount <= 0:
            continue  # skip refunds/credits
        name = (txn["name"] or "").upper()
        account_label = account_labels.get(txn["account_id"], "Other account")
        override = overrides.get(name)

        if override == "excluded":
            continue
        if override is None and any(kw in name for kw in excluded_keywords):
            continue  # transfer/payment/credit card bill, not discretionary spending

        if override:
            group, cat = override.split(".", 1)
        else:
            group = cat = None
            for g in ("variable_needs", "variable_wants"):
                for c, cfg in CATEGORIES[g].items():
                    if any(kw.upper() in name for kw in cfg["keywords"]):
                        group, cat = g, c
                        break
                if group:
                    break

        entry = {"name": txn["name"], "amount": amount, "account": account_label}
        if group is None:
            group, cat = "variable_wants", "misc_other"
            unmatched.append(entry)

        totals[group][cat] += amount
        detail[group][cat].append(entry)
        by_account.setdefault(account_label, []).append(
            {"name": txn["name"], "amount": amount, "category": cat}
        )

    return totals, detail, unmatched, by_account


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

def write_dashboard_data(totals, detail, unmatched, by_account, balances, account_labels, balances_by_id, meta_by_id, nancy_messages):
    period_start, period_end = current_pay_period()

    goals = dict(CATEGORIES["goals"])
    goals["actual_checking_balance"] = balances["checking_balance"]
    goals["actual_savings_balance"] = balances["savings_balance"]

    debt = json.loads(json.dumps(CATEGORIES["debt"]))  # deep copy, don't mutate CATEGORIES
    for debt_key, balance in balances["credit_balances"].items():
        if debt_key in debt:
            debt[debt_key]["current_balance"] = balance

    # Every linked account gets a card, even ones with no transactions this
    # pay period (e.g. a card only used for monthly debt payments) - the
    # transaction list just comes back empty for those. Keyed by account_id
    # (not display name) so two accounts that happen to share the same
    # Plaid-reported name (e.g. SoFi's checking/savings, or multiple cards
    # under one Amex login) don't collide and silently drop one another.
    accounts = [
        {
            "name": account_labels[aid],
            "balance": balance,
            "type": meta_by_id[aid]["type"],
            "subtype": meta_by_id[aid]["subtype"],
            "limit": meta_by_id[aid]["limit"],
            "transactions": by_account.get(account_labels[aid], []),
        }
        for aid, balance in balances_by_id.items()
    ]

    output = {
        "last_updated": date.today().isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "categories": totals,
        "categories_detail": detail,
        "unmatched_transactions": unmatched,
        "accounts": accounts,
        "caps": {
            group: {cat: cfg["biweekly_cap"] for cat, cfg in CATEGORIES[group].items()}
            for group in ("variable_needs", "variable_wants")
        },
        "goals": goals,
        "debt": debt,
        "nancy_messages": nancy_messages,
    }
    with open(DASHBOARD_DATA_PATH, "w") as f:
        json.dump(output, f, indent=2)

    return output


# ---------- Nancy's daily summary ----------

NANCY_SYSTEM_PROMPT = """You are "Nancy" - an extremely blunt, zero-excuses personal budget coach for
a hobby spending-tracker app. The name is just a nickname the user picked;
you are not roleplaying as any real public figure or influencer, do not
reference politics or anyone's real views/statements/biography - stay
entirely and only on the topic of this user's personal budget.

Tone: a hype, blunt bro - lots of "brah"/"bro", direct to the point of
bluntness, no hedging, no coddling, no "I'm not a financial advisor"
disclaimers - the user knows exactly what this is. You do not accept
excuses - if they're overspending, say so plainly and tell them what to
actually do about it, framed as accountability rather than cruelty. Short,
punchy sentences over long explanations. Genuinely enthusiastic and
complimentary when they're actually doing well - the bluntness cuts both
ways, it's not just criticism. ALWAYS start every message with the word
"Brah" as the very first word, no exceptions.

You are given the user's current pay-period spending data as JSON. Base your
summary strictly on those numbers - never invent figures. Keep it short - a
few sentences, not an essay."""


def generate_daily_summary(dashboard_data):
    if not ANTHROPIC_API_KEY:
        return None

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 300,
            "system": NANCY_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Current budget data (JSON):\n{json.dumps(dashboard_data)}\n\n"
                        "Write today's check-in summary."
                    ),
                }
            ],
        },
        timeout=30,
    )
    if not response.ok:
        print(f"Nancy summary failed: {response.status_code} {response.text[:200]}")
        return None

    return response.json()["content"][0]["text"]


# ---------- Main ----------

def main():
    accounts = get_all_accounts()
    excluded_account_ids = {a["account_id"] for a in accounts if is_excluded_account(a)}
    accounts = [a for a in accounts if a["account_id"] not in excluded_account_ids]

    transactions = get_pay_period_transactions()
    transactions = [t for t in transactions if t["account_id"] not in excluded_account_ids]

    account_labels, balances_by_id, meta_by_id = build_account_labels(accounts)
    balances = summarize_balances(accounts)

    totals, detail, unmatched, by_account = categorize(transactions, account_labels)
    alerts = check_caps_and_alert(totals)

    # Read any prior Nancy messages before write_dashboard_data overwrites
    # the file, so her summaries accumulate into one running thread the
    # dashboard chat widget can render - the same text that goes out as a
    # push notification also shows up as a message in the chat.
    nancy_messages = load_previous_nancy_messages()
    dashboard_data = write_dashboard_data(
        totals, detail, unmatched, by_account, balances, account_labels, balances_by_id, meta_by_id, nancy_messages
    )

    summary = None
    if should_send_nancy_summary():
        summary = generate_daily_summary(dashboard_data)
        if summary:
            nancy_messages.append({"text": summary, "date": date.today().isoformat()})
            dashboard_data["nancy_messages"] = nancy_messages[-15:]
            with open(DASHBOARD_DATA_PATH, "w") as f:
                json.dump(dashboard_data, f, indent=2)
            send_ntfy(summary, title="Nancy's Check-In", priority="default")

    print(f"Checked {len(transactions)} transactions.")
    print(f"Totals: {json.dumps(totals, indent=2)}")
    print(f"Balances: {json.dumps(balances, indent=2)}")
    print(f"Nancy summary sent: {bool(summary)}")
    if alerts:
        print(f"Sent {len(alerts)} alert(s).")
    else:
        print("No alerts needed - all categories within range.")


if __name__ == "__main__":
    main()
