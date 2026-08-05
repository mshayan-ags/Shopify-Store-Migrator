from flask import Blueprint, jsonify, redirect, render_template, url_for

from app import jobs as job_runner

bp = Blueprint("jobs", __name__)


@bp.route("/")
def index():
    job_runner.poll_all_running()
    return render_template("jobs/index.html", jobs=job_runner.list_jobs(limit=200))


@bp.route("/<int:job_id>")
def detail(job_id):
    job = job_runner.poll_job(job_id)
    if not job:
        return redirect(url_for("jobs.index"))
    log = job_runner.tail_log(job_id)
    return render_template("jobs/detail.html", job=job, log=log)


@bp.route("/<int:job_id>/status")
def status(job_id):
    job = job_runner.poll_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(
        {
            "status": job["status"],
            "log": job_runner.tail_log(job_id),
            "return_code": job.get("return_code"),
        }
    )


@bp.route("/<int:job_id>/kill", methods=["POST"])
def kill(job_id):
    job_runner.kill_job(job_id)
    return redirect(url_for("jobs.detail", job_id=job_id))
