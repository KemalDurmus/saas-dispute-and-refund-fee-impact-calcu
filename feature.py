from flask import Blueprint, flash, render_template, request

import logic

bp = Blueprint("feature", __name__)


def _default_form():
    a = logic.DEFAULT_ASSUMPTIONS
    return {"refunds_text": "", "disputes_text": "", "fee_pct": a["fee_pct"],
            "fee_fixed": a["fee_fixed"], "dispute_fee": a["dispute_fee"]}


def _num(name, default, lo, hi, label, form):
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw.replace(",", "."))
    except ValueError:
        flash("%s must be a number. The default (%s) was used." % (label, default), "warning")
        return default
    if val < lo or val > hi:
        flash("%s must be between %s and %s. The default (%s) was used." % (label, lo, hi, default), "warning")
        return default
    return val


def _read(kind, label):
    """Return (text or None, warning or None) from file upload or textarea."""
    f = request.files.get(kind + "_file")
    if f is not None and f.filename:
        data = f.read()
        return logic.decode_bytes(data, label)
    pasted = request.form.get(kind + "_text") or ""
    if pasted.strip():
        return logic.decode_bytes(pasted.encode("utf-8"), label)
    return None, None


@bp.route("/")
def index():
    return render_template("index.html", form=_default_form(), result=None, export_uri=None, demo=False)


@bp.route("/analyze", methods=["POST"])
def analyze():
    form = _default_form()
    form["fee_pct"] = _num("fee_pct", form["fee_pct"], 0, 100, "Processing fee %", request.form)
    form["fee_fixed"] = _num("fee_fixed", form["fee_fixed"], 0, 1000, "Fixed fee per refund", request.form)
    form["dispute_fee"] = _num("dispute_fee", form["dispute_fee"], 0, 10000, "Dispute fee", request.form)
    assumptions = {"fee_pct": form["fee_pct"], "fee_fixed": form["fee_fixed"], "dispute_fee": form["dispute_fee"]}
    demo = bool(request.form.get("demo"))
    try:
        extra = []
        if demo:
            refunds_text, disputes_text = logic.demo_data()
        else:
            refunds_text, w1 = _read("refunds", "refunds")
            disputes_text, w2 = _read("disputes", "disputes")
            extra = [w for w in (w1, w2) if w]
            form["refunds_text"] = request.form.get("refunds_text") or ""
            form["disputes_text"] = request.form.get("disputes_text") or ""
            if refunds_text is None and disputes_text is None:
                flash("Upload or paste at least one CSV (refunds or disputes), or click \"Try with sample data\".", "error")
                return render_template("index.html", form=form, result=None, export_uri=None, demo=False)
        result = logic.analyze(refunds_text, disputes_text, assumptions)
        result["warnings"] = extra + result["warnings"]
        uri = logic.export_csv_uri(result)
        return render_template("index.html", form=form, result=result, export_uri=uri, demo=demo)
    except logic.InputError as exc:
        flash(str(exc), "error")
    except Exception:
        flash("Something went wrong while reading this data. Please check that it is a Stripe CSV export and try again.", "error")
    return render_template("index.html", form=form, result=None, export_uri=None, demo=False)
