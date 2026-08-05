import json
import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import jobs
from transfer.transfer_all import STEPS, OPT_IN_STEPS

logger = logging.getLogger("app.dashboard")

bp = Blueprint("dashboard", __name__)

STEP_MODULES = {
    "pages": "transfer.transfer_pages",
    "blogs": "transfer.transfer_blogs",
    "products": "transfer.transfer_product",
    "collections": "transfer.transfer_collections",
    "customers": "transfer.transfer_customers",
    "customer_segments": "transfer.transfer_customer_segments",
    "orders": "transfer.transfer_orders",
    "draft_orders": "transfer.transfer_draft_orders",
    "navigation": "transfer.transfer_navigation",
    "redirects": "transfer.transfer_redirects",
    "metaobjects": "transfer.transfer_metaobjects",
    "store_metafields": "transfer.transfer_store_metafields",
    "policies": "transfer.transfer_policies",
    "locations": "transfer.transfer_locations",
    "markets": "transfer.transfer_markets",
    "discounts": "transfer.transfer_discounts",
    "selling_plans": "transfer.transfer_selling_plans",
    "b2b": "transfer.transfer_b2b",
    "files": "transfer.transfer_files",
    "theme": "transfer.transfer_theme",
    "shipping": "transfer.transfer_shipping",
    "translations": "transfer.transfer_translations",
    "gift_cards": "transfer.transfer_gift_cards",
}

STEPS_WITHOUT_XLSX = {"theme"}


@bp.route("/")
def index():
    jobs.poll_all_running()
    recent_jobs = jobs.list_jobs(limit=10)
    return render_template(
        "dashboard/index.html",
        steps=STEPS,
        opt_in_steps=OPT_IN_STEPS,
        recent_jobs=recent_jobs,
    )


def _build_step_args(step, form, execute):
    args = ["--out", "Results"]

    if form.get("xlsx") == "on" and step not in STEPS_WITHOUT_XLSX:
        args.append("--xlsx")

    if step == "products":
        if form.get("product_handle"):
            args += ["--product", form["product_handle"]]
        else:
            args.append("--all")
        if form.get("limit"):
            args += ["--limit", form["limit"]]
        if form.get("start_at"):
            args += ["--start-at", form["start_at"]]
        if form.get("workers"):
            args += ["--workers", form["workers"]]

    elif step == "collections":
        if form.get("collection"):
            args += ["--collection", form["collection"]]
        if form.get("initial_limit"):
            args += ["--initial-limit", form["initial_limit"]]
        if form.get("workers"):
            args += ["--workers", form["workers"]]

    elif step == "orders":
        if form.get("limit"):
            args += ["--limit", form["limit"]]
        if form.get("replace_existing") == "on":
            args.append("--replace-existing")
        if form.get("workers"):
            args += ["--workers", form["workers"]]

    elif step == "draft_orders":
        if form.get("limit"):
            args += ["--limit", form["limit"]]
        if form.get("workers"):
            args += ["--workers", form["workers"]]

    elif step == "discounts":
        if form.get("include_expired") == "on":
            args.append("--include-expired")

    elif step == "translations":
        if form.get("locale"):
            args += ["--locale", form["locale"]]

    elif step == "gift_cards":
        if form.get("confirm_gift_cards") == "on":
            args.append("--i-understand-this-creates-real-balance")

    elif step == "theme":
        if form.get("theme_all") == "on":
            args.append("--all")
        elif form.get("theme_id"):
            args += ["--theme-id", form["theme_id"]]
        elif form.get("theme_role"):
            args += ["--role", form["theme_role"]]
        if form.get("theme_name"):
            args += ["--name", form["theme_name"]]
        if form.get("publish") == "on":
            args.append("--publish")

    if execute:
        args.append("--execute")
    return args


def _build_all_args(form, execute):
    args = ["--out", "Results"]
    if execute:
        args.append("--execute")
    if form.get("xlsx") == "on":
        args.append("--xlsx")
    if form.get("product_limit"):
        args += ["--product-limit", form["product_limit"]]
    if form.get("order_limit"):
        args += ["--order-limit", form["order_limit"]]
    if form.get("workers"):
        args += ["--workers", form["workers"]]
    if form.get("include_expired_discounts") == "on":
        args.append("--include-expired-discounts")
    if form.get("translations_locale"):
        args += ["--translations-locale", form["translations_locale"]]
    return args


@bp.route("/run", methods=["POST"])
def run():
    step = request.form.get("step", "")
    execute = request.form.get("execute") == "on"
    confirm_gift_cards = request.form.get("confirm_gift_cards") == "on"

    try:
        source_connection = json.loads(request.form.get("source_connection_json") or "null")
        dest_connection = json.loads(request.form.get("dest_connection_json") or "null")
    except ValueError:
        source_connection = dest_connection = None

    if not source_connection or not dest_connection:
        flash("Choose both a source and destination connection", "error")
        return redirect(url_for("dashboard.index"))

    if step not in STEPS and step not in OPT_IN_STEPS and step != "all":
        flash(f"Unknown step '{step}'", "error")
        return redirect(url_for("dashboard.index"))

    if step == "all":
        module = "transfer.transfer_all"
        args = _build_all_args(request.form, execute)
    else:
        module = STEP_MODULES[step]
        args = _build_step_args(step, request.form, execute)
        if step == "gift_cards" and execute and not confirm_gift_cards:
            flash(
                "Gift cards create real redeemable balance on the destination -- "
                "check the confirmation box to proceed with --execute.",
                "error",
            )
            return redirect(url_for("dashboard.index"))

    label = f"{step} ({'execute' if execute else 'dry-run'})"
    logger.info("Dashboard launching step=%s module=%s args=%s", step, module, args)
    job_id = jobs.start_job(
        label=label,
        module=module,
        args=args,
        source_connection=source_connection,
        dest_connection=dest_connection,
    )
    flash(f"Started job #{job_id}: {label}", "success")
    return redirect(url_for("jobs.detail", job_id=job_id))
