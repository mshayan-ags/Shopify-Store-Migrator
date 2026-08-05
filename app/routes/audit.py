import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import jobs

bp = Blueprint("audit", __name__)

AUDIT_SCRIPTS = {
    "installed_apps": {"module": "audit.audit_installed_apps", "label": "Installed apps"},
    "payment_config": {"module": "audit.audit_payment_config", "label": "Payment configuration"},
    "staff_accounts": {"module": "audit.audit_staff_accounts", "label": "Staff accounts"},
    "tax_settings": {"module": "audit.audit_tax_settings", "label": "Tax settings"},
    "verify_products": {"module": "audit.verify_product_migration", "label": "Verify product migration"},
    "verify_store": {"module": "audit.verify_store_migration", "label": "Verify store migration"},
}

VERIFY_STORE_RESOURCES = [
    "collections", "pages", "blogs", "navigation", "customers", "redirects",
    "discounts", "orders", "policies", "locations", "draft_orders", "gift_cards",
    "b2b", "markets", "files", "selling_plans", "customer_segments",
]


@bp.route("/")
def index():
    return render_template("audit/index.html", scripts=AUDIT_SCRIPTS, verify_store_resources=VERIFY_STORE_RESOURCES)


@bp.route("/run", methods=["POST"])
def run():
    key = request.form.get("script", "")

    try:
        source_connection = json.loads(request.form.get("source_connection_json") or "null")
        dest_connection = json.loads(request.form.get("dest_connection_json") or "null")
    except ValueError:
        source_connection = dest_connection = None

    if key not in AUDIT_SCRIPTS:
        flash(f"Unknown audit script '{key}'", "error")
        return redirect(url_for("audit.index"))
    if not source_connection or not dest_connection:
        flash("Choose both a source and destination connection", "error")
        return redirect(url_for("audit.index"))

    script = AUDIT_SCRIPTS[key]
    args = ["--out", "Results"]

    if script["module"] == "audit.verify_product_migration":
        args = ["--out", "Results"]
        if request.form.get("limit"):
            args += ["--limit", request.form["limit"]]
        if request.form.get("start_at"):
            args += ["--start-at", request.form["start_at"]]
        if request.form.get("workers"):
            args += ["--workers", request.form["workers"]]
        if request.form.get("deep_images") == "on":
            args.append("--deep-images")
        args.append("--all")
    elif script["module"] == "audit.verify_store_migration":
        resource = request.form.get("resource", "")
        args = ["--out", "Results"]
        if resource and resource != "all":
            args += ["--resource", resource]
        else:
            args.append("--all")

    job_id = jobs.start_job(
        label=script["label"],
        module=script["module"],
        args=args,
        source_connection=source_connection,
        dest_connection=dest_connection,
    )
    flash(f"Started job #{job_id}: {script['label']}", "success")
    return redirect(url_for("jobs.detail", job_id=job_id))
