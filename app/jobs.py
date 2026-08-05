import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("app.jobs")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = DATA_DIR / "job_logs"
JOBS_DIR = DATA_DIR / "jobs"

_LOCK = threading.Lock()
_JOBS = {}
_PROCESSES = {}
_NEXT_ID = 1


def _job_file(job_id):
    return JOBS_DIR / f"{job_id}.json"


def _persist(job):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_job_file(job["id"]), "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)


def _load_all():
    global _NEXT_ID
    if not JOBS_DIR.exists():
        return
    for path in sorted(JOBS_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            logger.warning("Skipping unreadable job state file %s", path, exc_info=True)
            continue
        if job.get("status") == "running":
            job["status"] = "unknown"
        _JOBS[job["id"]] = job
        _NEXT_ID = max(_NEXT_ID, job["id"] + 1)
    if _JOBS:
        logger.info("Restored %d job(s) from %s", len(_JOBS), JOBS_DIR)


_load_all()


def env_for_connection(conn, role):
    if not conn:
        return {}
    kind = conn.get("kind")
    config = conn.get("config") or {}

    if kind == "shopify":
        prefix = "SRC" if role == "src" else "DEST"
        shop = (conn.get("shop_domain") or "").strip()
        if shop.lower().endswith(".myshopify.com"):
            shop = shop[: -len(".myshopify.com")]
        return {
            f"{prefix}_SHOPIFY_SHOP": shop,
            f"{prefix}_SHOPIFY_ACCESS_TOKEN": conn.get("access_token") or "",
            f"{prefix}_SHOPIFY_CLIENT_ID": config.get("client_id", ""),
            f"{prefix}_SHOPIFY_CLIENT_SECRET": config.get("client_secret", ""),
        }
    if kind == "wix":
        return {
            "WIX_API_KEY": config.get("api_key", ""),
            "WIX_SITE_ID": config.get("site_id", ""),
        }
    if kind == "bigcommerce":
        return {
            "BIGCOMMERCE_STORE_HASH": config.get("store_hash", ""),
            "BIGCOMMERCE_ACCESS_TOKEN": config.get("access_token", ""),
            "BIGCOMMERCE_CLIENT_ID": config.get("client_id", ""),
        }
    if kind == "postgresql":
        if config.get("database_url"):
            return {"DATABASE_URL": config["database_url"]}
        return {
            "PG_HOST": config.get("host", ""),
            "PG_PORT": str(config.get("port", "5432")),
            "PG_DATABASE": config.get("database", ""),
            "PG_USER": config.get("user", ""),
            "PG_PASSWORD": config.get("password", ""),
        }
    if kind == "mongodb":
        return {
            "MONGO_URI": config.get("uri", ""),
            "MONGO_DATABASE": config.get("database", ""),
        }
    return {}


def start_job(label, module, args, source_connection=None, dest_connection=None, extra_env=None):
    global _NEXT_ID
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with _LOCK:
        job_id = _NEXT_ID
        _NEXT_ID += 1

    log_path = LOG_DIR / f"{job_id}.log"

    env = dict(os.environ)
    if source_connection:
        env.update(env_for_connection(source_connection, "src"))
    if dest_connection:
        env.update(env_for_connection(dest_connection, "dest"))
    if extra_env:
        env.update(extra_env)

    job = {
        "id": job_id,
        "label": label,
        "module": module,
        "args": args,
        "status": "running",
        "pid": None,
        "log_path": str(log_path),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "return_code": None,
    }

    cmd = [sys.executable, "-m", module, *args]
    log_fh = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    job["pid"] = process.pid
    _JOBS[job_id] = job
    _PROCESSES[job_id] = {"process": process, "log_fh": log_fh}
    _persist(job)
    logger.info("Started job #%d (pid %d): %s -m %s %s", job_id, process.pid, sys.executable, module, " ".join(args))
    return job_id


def poll_job(job_id):
    job = _JOBS.get(job_id)
    if not job:
        return None
    if job["status"] != "running":
        return job

    entry = _PROCESSES.get(job_id)
    if entry is None:
        return job

    return_code = entry["process"].poll()
    if return_code is not None:
        entry["log_fh"].close()
        _PROCESSES.pop(job_id, None)
        job["status"] = "success" if return_code == 0 else "failed"
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job["return_code"] = return_code
        _persist(job)
        logger.info("Job #%d finished with status=%s (return code %s)", job_id, job["status"], return_code)
    return job


def kill_job(job_id):
    entry = _PROCESSES.get(job_id)
    if entry is None:
        logger.warning("Kill requested for job #%d but it has no tracked process (already finished or unknown)", job_id)
        return False
    logger.info("Killing job #%d (pid %d) on request", job_id, entry["process"].pid)
    entry["process"].terminate()
    try:
        entry["process"].wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("Job #%d did not terminate within 10s, sending SIGKILL", job_id)
        entry["process"].kill()
    entry["log_fh"].close()
    _PROCESSES.pop(job_id, None)
    job = _JOBS.get(job_id)
    if job:
        job["status"] = "killed"
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _persist(job)
    return True


def tail_log(job_id, lines=300):
    job = _JOBS.get(job_id)
    if not job or not job.get("log_path"):
        return ""
    path = Path(job["log_path"])
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.readlines()
    return "".join(content[-lines:])


def get_job(job_id):
    return _JOBS.get(job_id)


def list_jobs(limit=200):
    return sorted(_JOBS.values(), key=lambda j: j["id"], reverse=True)[:limit]


def list_running_jobs():
    return [j for j in _JOBS.values() if j["status"] == "running"]


def poll_all_running():
    for job in list_running_jobs():
        poll_job(job["id"])
