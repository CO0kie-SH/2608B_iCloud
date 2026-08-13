from __future__ import annotations

"""
进程内收信任务注册表。

IMAP 连接一次可能耗时数秒到数十秒，所以收信走后台线程 + 前端轮询，
避免 HTTP 请求长时间挂住。
"""

import threading
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from tools.db import AliasDB, utc_now

# 并发上限 2：同一账户的 IMAP 连接数有限，开太多容易被服务端限流
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mail-sync")
_PRODUCTION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hme-production")
_LOCK = threading.Lock()
_MAX_JOBS = 50


@dataclass
class SyncJob:
    job_id: str
    account: str = ""
    status: str = "pending"  # pending / running / done / error
    started_at: str = ""
    finished_at: str = ""
    progress: list[str] = field(default_factory=list)
    stats: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "account": self.account,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": list(self.progress),
            "stats": list(self.stats),
            "error": self.error,
        }


_JOBS: dict[str, SyncJob] = {}


@dataclass
class ProductionJob:
    job_id: str
    account: str
    interface: str
    status: str = "pending"
    started_at: str = ""
    finished_at: str = ""
    progress: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "account": self.account,
            "interface": self.interface,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": list(self.progress),
            "result": dict(self.result),
            "error": self.error,
        }


_PRODUCTION_JOBS: dict[str, ProductionJob] = {}


def _trim_jobs() -> None:
    if len(_JOBS) <= _MAX_JOBS:
        return
    finished = [j for j in _JOBS.values() if j.status in ("done", "error")]
    finished.sort(key=lambda j: j.finished_at or j.started_at)
    for job in finished[: max(0, len(_JOBS) - _MAX_JOBS)]:
        _JOBS.pop(job.job_id, None)


def get_job(job_id: str) -> SyncJob | None:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        jobs = list(_JOBS.values())
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return [j.to_dict() for j in jobs]


def submit_sync(
    *,
    account_label: str,
    runner: Any,
    deduplicate: bool = False,
) -> SyncJob:
    """
    runner(on_progress) -> list[SyncStats]

    runner 内部自行构建服务与账户列表；本模块只负责状态与线程。
    """
    with _LOCK:
        if deduplicate:
            existing = next(
                (
                    item
                    for item in _JOBS.values()
                    if item.account == account_label and item.status in ("pending", "running")
                ),
                None,
            )
            if existing:
                return existing
        job = SyncJob(job_id=uuid.uuid4().hex[:12], account=account_label, status="pending")
        _JOBS[job.job_id] = job
        _trim_jobs()

    def _on_progress(message: str) -> None:
        with _LOCK:
            job.progress.append(f"{utc_now()} {message}")
            if len(job.progress) > 200:
                del job.progress[:-200]

    def _run() -> None:
        with _LOCK:
            job.status = "running"
            job.started_at = utc_now()
        try:
            stats = runner(_on_progress)
            with _LOCK:
                job.stats = [
                    s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in stats
                ]
                failed = [s for s in job.stats if not s.get("ok", True)]
                job.status = "done"
                if failed and not any(
                    s.get("saved") or s.get("updated") for s in job.stats
                ):
                    job.status = "error"
                    job.error = "; ".join(
                        f"{s.get('mailbox', '?')}: {s.get('error', '')}" for s in failed
                    )
        except Exception as e:
            with _LOCK:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
        finally:
            with _LOCK:
                job.finished_at = utc_now()

    _EXECUTOR.submit(_run)
    return job


def get_production_job(job_id: str, db: AliasDB | None = None) -> ProductionJob | dict[str, Any] | None:
    with _LOCK:
        job = _PRODUCTION_JOBS.get(job_id)
    return job or (db.get_production_job(job_id) if db else None)


def list_production_jobs(db: AliasDB | None = None) -> list[dict[str, Any]]:
    if db:
        return db.list_production_jobs()
    with _LOCK:
        jobs = sorted(_PRODUCTION_JOBS.values(), key=lambda j: j.started_at, reverse=True)
        return [j.to_dict() for j in jobs]


def submit_production(
    *, account_label: str, interface: str, requested: int, db: AliasDB, runner: Any
) -> ProductionJob:
    job = ProductionJob(
        job_id=uuid.uuid4().hex[:12], account=account_label, interface=interface
    )
    with _LOCK:
        _PRODUCTION_JOBS[job.job_id] = job
    db.create_production_job(job.job_id, account_label, interface, requested)

    def progress(message: str) -> None:
        with _LOCK:
            job.progress.append(f"{utc_now()} {message}")
            if len(job.progress) > 200:
                del job.progress[:-200]
            db.update_production_job(
                job.job_id, progress_json=json.dumps(job.progress, ensure_ascii=False)
            )

    def run() -> None:
        with _LOCK:
            job.status = "running"
            job.started_at = utc_now()
            db.update_production_job(
                job.job_id, status=job.status, started_at=job.started_at
            )
        try:
            result = runner(progress)
            with _LOCK:
                job.result = {
                    "requested": result.requested,
                    "threads": result.threads,
                    "created": result.created,
                    "items": result.items,
                    "errors": result.errors,
                }
                job.status = "done" if result.created or not result.errors else "error"
                if result.errors:
                    job.error = "; ".join(result.errors)
                db.update_production_job(
                    job.job_id,
                    status=job.status,
                    created=result.created,
                    result_json=json.dumps(job.result, ensure_ascii=False),
                    error=job.error,
                )
        except Exception as exc:
            with _LOCK:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                db.update_production_job(job.job_id, status=job.status, error=job.error)
        finally:
            with _LOCK:
                job.finished_at = utc_now()
                db.update_production_job(job.job_id, finished_at=job.finished_at)

    _PRODUCTION_EXECUTOR.submit(run)
    return job
