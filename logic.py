"""Core logic: parse Stripe refunds / disputes CSV exports and compute losses. Pure Python."""
import csv
import io
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

MAX_BYTES = 2_000_000
MAX_ROWS = 20000
NO_EMAIL = "(no email)"

DEFAULT_ASSUMPTIONS = {"fee_pct": 2.9, "fee_fixed": 0.30, "dispute_fee": 15.00}

REFUND_AMOUNT_COLS = ["amountrefunded", "refundamount", "refunded", "amount"]
DISPUTE_AMOUNT_COLS = ["disputedamount", "disputeamount", "amount"]
EMAIL_COLS = ["customeremail", "email", "billingdetailsemail", "receiptemail"]
FEE_COLS = ["fee", "fees", "processingfee", "disputefee", "stripefee"]
STATUS_COLS = ["status"]
DATE_COLS = ["createdutc", "created", "date", "createddate"]
COUNTRY_COLS = ["cardaddresscountry", "customercountry", "billingcountry", "cardcountry", "country"]
CURRENCY_COLS = ["currency"]


class InputError(ValueError):
    """Raised when user supplied data cannot be used."""


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def decode_bytes(data, label):
    """Return (text, warning_or_None)."""
    if not data or not data.strip():
        raise InputError("The %s input is empty." % label)
    if b"\x00" in data[:8192]:
        raise InputError(
            "The %s file doesn't look like text (it may be an XLSX, PDF or other binary file). "
            "Please export it from Stripe as CSV." % label)
    warning = None
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
        cut = data.rfind(b"\n")
        if cut > 0:
            data = data[:cut]
        warning = "The %s input was larger than 2 MB, so only the first part was analyzed." % label
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text, warning


def parse_money(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^0-9.,]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = parts[0] + "." + parts[1]
        else:
            s = s.replace(",", "")
    try:
        return abs(Decimal(s))
    except InvalidOperation:
        return None


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def parse_rows(text, kind, label, assumptions):
    """Parse one CSV text. Returns (rows, notes dict)."""
    first_line = text.lstrip().split("\n", 1)[0]
    delim = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    try:
        fields = reader.fieldnames
    except csv.Error:
        fields = None
    if not fields:
        raise InputError("The %s data has no header row. Paste or upload the CSV exactly as exported from Stripe." % label)
    norm = {}
    for f in fields:
        if f:
            norm.setdefault(_norm(f), f)

    def pick(cands):
        for c in cands:
            if c in norm:
                return norm[c]
        return None

    amount_col = pick(REFUND_AMOUNT_COLS if kind == "refund" else DISPUTE_AMOUNT_COLS)
    if not amount_col:
        shown = ", ".join([f for f in fields if f][:10]) or "none"
        raise InputError("The %s data has no amount column, so it doesn't look like a Stripe export. Columns found: %s." % (label, shown))
    cols = {
        "email": pick(EMAIL_COLS), "fee": pick(FEE_COLS), "status": pick(STATUS_COLS),
        "date": pick(DATE_COLS), "country": pick(COUNTRY_COLS), "currency": pick(CURRENCY_COLS),
    }
    rows = []
    notes = {"bad": 0, "failed": 0, "truncated": False, "no_email_col": cols["email"] is None}
    count = 0
    try:
        for rec in reader:
            count += 1
            if count > MAX_ROWS:
                notes["truncated"] = True
                break
            amount = parse_money(rec.get(amount_col))
            if amount is None:
                notes["bad"] += 1
                continue
            status = (rec.get(cols["status"]) or "").strip().lower() if cols["status"] else ""
            if kind == "refund" and status in ("failed", "canceled", "cancelled"):
                notes["failed"] += 1
                continue
            email = (rec.get(cols["email"]) or "").strip().lower() if cols["email"] else ""
            if "@" not in email:
                email = NO_EMAIL
            fee = parse_money(rec.get(cols["fee"])) if cols["fee"] else None
            fee_assumed = fee is None
            if fee is None:
                if kind == "refund":
                    fee = amount * Decimal(str(assumptions["fee_pct"])) / Decimal(100) + Decimal(str(assumptions["fee_fixed"]))
                else:
                    fee = Decimal(str(assumptions["dispute_fee"]))
            date_txt = (rec.get(cols["date"]) or "").strip()[:10] if cols["date"] else ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_txt):
                date_txt = ""
            country = (rec.get(cols["country"]) or "").strip().upper() if cols["country"] else ""
            currency = (rec.get(cols["currency"]) or "").strip().upper() if cols["currency"] else ""
            rows.append({"kind": kind, "email": email, "amount": amount, "fee": fee, "fee_assumed": fee_assumed,
                         "status": status, "date": date_txt, "country": country, "currency": currency})
    except csv.Error:
        raise InputError("The %s data could not be read as CSV. Please check the file format." % label)
    if not rows:
        raise InputError("No readable rows were found in the %s data (%s skipped because the amount was missing or invalid)." % (label, plural(notes["bad"] + notes["failed"], "row")))
    return rows, notes


def _dispute_outcome(status):
    if status == "lost":
        return "lost"
    if status in ("won", "charge_refunded"):
        return "resolved"
    return "open"


def analyze(refund_text, dispute_text, assumptions=None):
    a = dict(DEFAULT_ASSUMPTIONS)
    if assumptions:
        a.update(assumptions)
    warnings = []
    all_rows = []
    for text, kind, label in ((refund_text, "refund", "refunds"), (dispute_text, "dispute", "disputes")):
        if text is None or not text.strip():
            continue
        rows, notes = parse_rows(text, kind, label, a)
        all_rows.extend(rows)
        if notes["bad"]:
            warnings.append("%s in the %s data had a missing or invalid amount and %s skipped." % (plural(notes["bad"], "row"), label, "was" if notes["bad"] == 1 else "were"))
        if notes["failed"]:
            warnings.append("%s in the refunds data had a failed or canceled status and %s ignored." % (plural(notes["failed"], "row"), "was" if notes["failed"] == 1 else "were"))
        if notes["truncated"]:
            warnings.append("The %s data has more than %d rows; only the first %d were analyzed." % (label, MAX_ROWS, MAX_ROWS))
        if notes["no_email_col"]:
            warnings.append("The %s data has no customer email column, so those rows can't be attributed to customers." % label)
    if not all_rows:
        raise InputError("Please provide a refunds CSV, a disputes CSV, or both.")

    cur_counts = Counter(r["currency"] for r in all_rows if r["currency"])
    currency = cur_counts.most_common(1)[0][0] if cur_counts else "USD"
    other = [r for r in all_rows if r["currency"] and r["currency"] != currency]
    if other:
        warnings.append("%s in other currencies %s skipped. Amounts below are in %s only; export and analyze each currency separately." % (plural(len(other), "row"), "was" if len(other) == 1 else "were", currency))
    rows = [r for r in all_rows if not r["currency"] or r["currency"] == currency]

    t = {"refund_count": 0, "refund_amount": Decimal(0), "refund_fees": Decimal(0),
         "dispute_count": 0, "dispute_lost": Decimal(0), "dispute_open": Decimal(0),
         "dispute_open_count": 0, "dispute_lost_count": 0, "dispute_fees": Decimal(0)}
    cust = defaultdict(lambda: {"refunds": 0, "disputes": 0, "refunded": Decimal(0), "lost": Decimal(0), "open": Decimal(0), "fees": Decimal(0)})
    ctry = defaultdict(lambda: {"events": 0, "loss": Decimal(0)})
    dates = []
    unattributed = 0
    assumed = 0
    for r in rows:
        loss = Decimal(0)
        c = cust[r["email"]]
        if r["kind"] == "refund":
            t["refund_count"] += 1
            t["refund_amount"] += r["amount"]
            t["refund_fees"] += r["fee"]
            c["refunds"] += 1
            c["refunded"] += r["amount"]
            loss = r["amount"] + r["fee"]
        else:
            t["dispute_count"] += 1
            t["dispute_fees"] += r["fee"]
            c["disputes"] += 1
            out = _dispute_outcome(r["status"])
            if out == "lost":
                t["dispute_lost"] += r["amount"]
                t["dispute_lost_count"] += 1
                c["lost"] += r["amount"]
                loss = r["amount"] + r["fee"]
            elif out == "open":
                t["dispute_open"] += r["amount"]
                t["dispute_open_count"] += 1
                c["open"] += r["amount"]
                loss = r["fee"]
            else:
                loss = r["fee"]
        c["fees"] += r["fee"]
        if r["fee_assumed"]:
            assumed += 1
        if r["email"] == NO_EMAIL:
            unattributed += 1
        if r["country"]:
            ctry[r["country"]]["events"] += 1
            ctry[r["country"]]["loss"] += loss
        if r["date"]:
            dates.append(r["date"])

    fees_total = t["refund_fees"] + t["dispute_fees"]
    total_loss = t["refund_amount"] + t["dispute_lost"] + fees_total

    customers = []
    for email, c in cust.items():
        if email == NO_EMAIL:
            continue
        events = c["refunds"] + c["disputes"]
        flags = []
        if c["disputes"] >= 2:
            flags.append("Repeat disputer")
        if c["refunds"] >= 2:
            flags.append("Repeat refunds")
        if c["disputes"] >= 1 and c["refunds"] >= 1:
            flags.append("Refund + dispute")
        total = c["refunded"] + c["lost"] + c["fees"]
        customers.append({
            "email": email, "refunds": c["refunds"], "disputes": c["disputes"], "events": events,
            "refunded": float(round(c["refunded"], 2)), "lost": float(round(c["lost"], 2)),
            "fees": float(round(c["fees"], 2)), "total": float(round(total, 2)),
            "flags": flags, "repeat": events >= 2,
        })
    customers.sort(key=lambda x: (-x["total"], x["email"]))
    repeat = [c for c in customers if c["repeat"]]
    repeat.sort(key=lambda x: (-x["events"], -x["total"], x["email"]))

    countries = [{"country": k, "events": v["events"], "loss": float(round(v["loss"], 2))} for k, v in ctry.items()]
    countries.sort(key=lambda x: (-x["loss"], x["country"]))

    if unattributed:
        warnings.append("%s had no valid customer email and %s left out of the customer table (still counted in totals)." % (plural(unattributed, "row"), "was" if unattributed == 1 else "were"))
    if assumed:
        warnings.append("%s had no fee value in the CSV, so the fee assumptions you set were used for them." % plural(assumed, "row"))

    def f2(x):
        return float(round(x, 2))

    return {
        "currency": currency,
        "period": (min(dates), max(dates)) if dates else None,
        "total_loss": f2(total_loss),
        "fees_total": f2(fees_total),
        "refund_count": t["refund_count"], "refund_amount": f2(t["refund_amount"]), "refund_fees": f2(t["refund_fees"]),
        "dispute_count": t["dispute_count"], "dispute_lost": f2(t["dispute_lost"]), "dispute_lost_count": t["dispute_lost_count"],
        "dispute_open": f2(t["dispute_open"]), "dispute_open_count": t["dispute_open_count"], "dispute_fees": f2(t["dispute_fees"]),
        "customers": customers, "repeat": repeat, "countries": countries,
        "customer_count": len(customers), "warnings": warnings,
    }


def _safe(cell):
    s = str(cell)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv_uri(result):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["customer_email", "refunds", "disputes", "refunded_amount", "lost_dispute_amount", "fees", "total_loss", "currency", "flags"])
    for c in result["customers"]:
        w.writerow([_safe(c["email"]), c["refunds"], c["disputes"], "%.2f" % c["refunded"], "%.2f" % c["lost"],
                    "%.2f" % c["fees"], "%.2f" % c["total"], result["currency"], "; ".join(c["flags"])])
    return "data:text/csv;charset=utf-8," + quote(out.getvalue())


def demo_data():
    """Embedded sample exports with dates relative to today. Includes a Fee column."""
    now = datetime.utcnow()

    def d(days):
        return (now - timedelta(days=days)).strftime("%Y-%m-%d 10:15")

    refunds = [
        ("re_1001", 4, 49.00, "anna@fastmail.example", "succeeded", "requested_by_customer", "US"),
        ("re_1002", 9, 99.00, "mike@devshop.example", "succeeded", "requested_by_customer", "US"),
        ("re_1003", 13, 49.00, "anna@fastmail.example", "succeeded", "duplicate", "US"),
        ("re_1004", 18, 199.00, "li@studio.example", "succeeded", "requested_by_customer", "GB"),
        ("re_1005", 22, 29.00, "tom@mailbox.example", "succeeded", "requested_by_customer", "DE"),
        ("re_1006", 27, 99.00, "mike@devshop.example", "succeeded", "requested_by_customer", "US"),
        ("re_1007", 31, 29.00, "sara@startup.example", "succeeded", "requested_by_customer", "CA"),
        ("re_1008", 36, 49.00, "jo@agency.example", "succeeded", "fraudulent", "DE"),
        ("re_1009", 41, 99.00, "nina@gmail.example", "succeeded", "requested_by_customer", "GB"),
        ("re_1010", 47, 29.00, "mike@devshop.example", "succeeded", "requested_by_customer", "US"),
    ]
    disputes = [
        ("dp_2001", 6, 99.00, "mike@devshop.example", "lost", "fraudulent", "US"),
        ("dp_2002", 15, 199.00, "zed@proton.example", "lost", "fraudulent", "DE"),
        ("dp_2003", 24, 49.00, "zed@proton.example", "lost", "product_not_received", "DE"),
        ("dp_2004", 33, 99.00, "li@studio.example", "won", "general", "GB"),
        ("dp_2005", 3, 49.00, "rob@inbox.example", "needs_response", "fraudulent", "US"),
        ("dp_2006", 52, 29.00, "nina@gmail.example", "lost", "credit_not_processed", "GB"),
    ]
    rt = ["id,Created (UTC),Amount Refunded,Currency,Customer Email,Status,Reason,Card Address Country,Fee"]
    for i, days, amt, em, st, rs, co in refunds:
        fee = amt * 0.029 + 0.30
        rt.append("%s,%s,%.2f,usd,%s,%s,%s,%s,%.2f" % (i, d(days), amt, em, st, rs, co, fee))
    dt = ["id,Created (UTC),Amount,Currency,Customer Email,Status,Reason,Card Address Country,Fee"]
    for i, days, amt, em, st, rs, co in disputes:
        dt.append("%s,%s,%.2f,usd,%s,%s,%s,%s,15.00" % (i, d(days), amt, em, st, rs, co))
    return "\n".join(rt) + "\n", "\n".join(dt) + "\n"
