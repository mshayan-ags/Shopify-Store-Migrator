import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import jobs

bp = Blueprint("sources", __name__)

CONNECTOR_MODULES = {
    "wix": "sources.wix.main",
    "bigcommerce": "sources.bigcommerce.main",
    "postgresql": "sources.postgresql.main",
    "mongodb": "sources.mongodb.main",
}

RESOURCE_CHOICES = {
    "wix": ["all", "products", "collections", "customers", "orders", "discounts", "pages"],
    "bigcommerce": ["all", "products", "collections", "customers", "orders", "discounts", "pages"],
    "postgresql": ["all", "products", "collections", "customers", "orders"],
    "mongodb": ["all", "products", "collections", "customers", "orders"],
}


@bp.route("/")
def index():
    return render_template("sources/index.html", resource_choices=RESOURCE_CHOICES)


@bp.route("/run", methods=["POST"])
def run():
    resource = request.form.get("resource", "all")

    try:
        connection = json.loads(request.form.get("connection_json") or "null")
    except ValueError:
        connection = None

    if not connection or connection.get("kind") not in CONNECTOR_MODULES:
        flash("Choose a valid non-Shopify source connection", "error")
        return redirect(url_for("sources.index"))

    kind = connection["kind"]
    config = connection.get("config") or {}
    args = ["--resource", resource, "--out", "Results"]

    if kind in ("postgresql", "mongodb"):
        mapping_path = config.get("mapping_path")
        if not mapping_path:
            flash(f"This {kind} connection needs a mapping YAML path set first (edit the connection)", "error")
            return redirect(url_for("connections.index"))
        args = ["--mapping", mapping_path] + args

    job_id = jobs.start_job(
        label=f"{kind} export: {resource}",
        module=CONNECTOR_MODULES[kind],
        args=args,
        source_connection=connection,
    )
    flash(f"Started job #{job_id}", "success")
    return redirect(url_for("jobs.detail", job_id=job_id))
