from __future__ import annotations

import threading
import time
from typing import Any, Callable

from tools.client import CookieInvalidError
from tools.db import AliasDB, unix_now, utc_now
from tools.rate_limit import HMECreateRateLimitError
from web.deps import get_accounts, get_db, get_settings
from web.jobs import get_production_job
from web.production_service import submit_account_production


class ProductionLoopController:
    """持久化的单线程轮询生产控制器，执行顺序与 produce.bat 一致。"""

    def __init__(
        self,
        db: AliasDB,
        *,
        accounts_loader: Callable[[], list[Any]],
        settings_loader: Callable[[], Any],
        submitter: Callable[..., Any] = submit_account_production,
        job_getter: Callable[..., Any] = get_production_job,
        poll_interval_sec: float = 2.0,
        clock: Callable[[], int] = unix_now,
    ) -> None:
        self.db = db
        self.accounts_loader = accounts_loader
        self.settings_loader = settings_loader
        self.submitter = submitter
        self.job_getter = job_getter
        self.poll_interval_sec = max(0.01, float(poll_interval_sec))
        self.clock = clock
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._thread: threading.Thread | None = None

    def startup(self) -> dict[str, Any]:
        with self._lock:
            interrupted = self.db.mark_incomplete_production_jobs()
            state = self.db.get_production_loop_state()
            if not state.get("enabled"):
                if state.get("status") in {"running", "stopping"}:
                    state = self.db.update_production_loop_state(
                        status="stopped", current_account="", current_job_id="", next_run_at=0
                    )
                return state
            if self._deadline_hit(state):
                return self.db.update_production_loop_state(
                    enabled=False,
                    status="completed",
                    current_account="",
                    current_job_id="",
                    next_run_at=0,
                )
            self.db.update_production_loop_state(
                status="running", current_account="", current_job_id="", next_run_at=0
            )
            message = "服务启动后自动恢复轮询"
            if interrupted:
                message += f"，已整理 {interrupted} 个中断任务"
            self._append_progress(message)
            self._launch_locked()
            return self.snapshot()

    def shutdown(self) -> None:
        self._shutdown_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(3.0, self.poll_interval_sec + 1.0))

    def configure(
        self,
        *,
        selected_accounts: list[str],
        interface: str,
        mode: str,
        duration_minutes: int,
        interval_sec: int,
    ) -> dict[str, Any]:
        known = {item.name for item in self.accounts_loader()}
        selected: list[str] = []
        for raw in selected_accounts:
            name = str(raw or "").strip()
            if name and name not in selected:
                if name not in known:
                    raise ValueError(f"未知账户: {name}")
                selected.append(name)
        interface = str(interface or "legacy")
        mode = str(mode or "forever")
        duration_minutes = int(duration_minutes or 0)
        interval_sec = int(interval_sec or 0)
        if interface != "legacy":
            raise ValueError("未知生产接口")
        if mode not in {"forever", "timed"}:
            raise ValueError("运行模式必须是 forever 或 timed")
        if mode == "timed" and duration_minutes < 1:
            raise ValueError("定时运行分钟数必须大于 0")
        if interval_sec < 1 or interval_sec > 60:
            raise ValueError("轮次间隔范围为 1-60 秒")

        with self._lock:
            state = self.db.get_production_loop_state()
            active = bool(state.get("enabled")) or state.get("status") in {
                "running",
                "stopping",
            }
            if active:
                locked = {
                    "interface": interface,
                    "mode": mode,
                    "duration_minutes": duration_minutes if mode == "timed" else 0,
                    "interval_sec": interval_sec,
                }
                for key, value in locked.items():
                    if state.get(key) != value:
                        raise ValueError("轮询运行期间只允许修改参与账号")
            updated = self.db.update_production_loop_state(
                selected_accounts=selected,
                interface=interface,
                mode=mode,
                duration_minutes=duration_minutes if mode == "timed" else 0,
                interval_sec=interval_sec,
            )
            if active:
                self._append_progress(f"参与账号已更新，下轮生效：{len(selected)} 个")
            return updated

    def start(self) -> dict[str, Any]:
        with self._lock:
            state = self.db.get_production_loop_state()
            if state.get("enabled") and self._thread and self._thread.is_alive():
                return self.snapshot()
            selected = list(state.get("selected_accounts") or [])
            if not selected:
                raise ValueError("请至少勾选一个参与生产的账号")
            now = self.clock()
            mode = state.get("mode") or "forever"
            duration = int(state.get("duration_minutes") or 0)
            if mode == "timed" and duration < 1:
                raise ValueError("定时运行分钟数必须大于 0")
            deadline = now + duration * 60 if mode == "timed" else 0
            self._stop_event.clear()
            self._shutdown_event.clear()
            self.db.update_production_loop_state(
                enabled=True,
                status="running",
                started_at=utc_now(),
                deadline_at=deadline,
                current_account="",
                current_job_id="",
                next_run_at=0,
                round_no=0,
                submitted=0,
                created=0,
                failed=0,
                skipped=0,
                progress=[],
                last_error="",
            )
            self._append_progress(
                f"轮询已启动，模式={mode}，账号={len(selected)}，接口=legacy"
            )
            self._launch_locked()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            state = self.db.get_production_loop_state()
            running = bool(self._thread and self._thread.is_alive())
            status = "stopping" if running else "stopped"
            self.db.update_production_loop_state(enabled=False, status=status, next_run_at=0)
            if running:
                self._append_progress("已请求停止，当前任务完成后结束")
            elif state.get("status") != "stopped":
                self._append_progress("轮询已停止")
            self._stop_event.set()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        state = self.db.get_production_loop_state()
        state["thread_alive"] = bool(self._thread and self._thread.is_alive())
        job_id = str(state.get("current_job_id") or "")
        current_job = self.job_getter(job_id, self.db) if job_id else None
        if current_job is not None and hasattr(current_job, "to_dict"):
            current_job = current_job.to_dict()
        state["current_job"] = current_job
        return state

    def _launch_locked(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hme-production-loop",
            daemon=True,
        )
        self._thread.start()

    def _append_progress(self, message: str) -> None:
        with self._lock:
            state = self.db.get_production_loop_state()
            progress = list(state.get("progress") or [])
            progress.append(f"{utc_now()} {message}")
            self.db.update_production_loop_state(progress=progress[-200:])

    def _increment(self, **deltas: int) -> dict[str, Any]:
        with self._lock:
            state = self.db.get_production_loop_state()
            changes = {
                key: int(state.get(key) or 0) + int(value)
                for key, value in deltas.items()
            }
            return self.db.update_production_loop_state(**changes)

    def _deadline_hit(self, state: dict[str, Any] | None = None) -> bool:
        current = state or self.db.get_production_loop_state()
        deadline = int(current.get("deadline_at") or 0)
        return bool(current.get("mode") == "timed" and deadline and self.clock() >= deadline)

    @staticmethod
    def _min_wait(current: int, candidate: int) -> int:
        candidate = max(1, int(candidate or 1))
        return candidate if current <= 0 else min(current, candidate)

    def _terminal_job(self, job_id: str) -> dict[str, Any]:
        while True:
            job = self.job_getter(job_id, self.db)
            if job is None:
                return {"status": "error", "error": f"生产任务不存在: {job_id}", "result": {}}
            data = job.to_dict() if hasattr(job, "to_dict") else dict(job)
            if data.get("status") not in {"pending", "running"}:
                return data
            time.sleep(self.poll_interval_sec)

    def _run(self) -> None:
        finish_status = "stopped"
        try:
            while True:
                state = self.db.get_production_loop_state()
                if self._shutdown_event.is_set():
                    return
                if self._stop_event.is_set() or not state.get("enabled"):
                    finish_status = "stopped"
                    break
                if self._deadline_hit(state):
                    finish_status = "completed"
                    self._append_progress("已到定时截止时间")
                    break

                selected = list(state.get("selected_accounts") or [])
                round_no = int(state.get("round_no") or 0) + 1
                self.db.update_production_loop_state(round_no=round_no, next_run_at=0)
                self._append_progress(f"第 {round_no} 轮开始，参与账号 {len(selected)} 个")
                accounts = {item.name: item for item in self.accounts_loader()}
                next_wait = 0

                for name in selected:
                    if self._shutdown_event.is_set():
                        return
                    if self._stop_event.is_set() or not self.db.get_production_loop_state().get("enabled"):
                        break
                    if self._deadline_hit():
                        finish_status = "completed"
                        break

                    account = accounts.get(name)
                    if account is None:
                        self._increment(skipped=1)
                        self._append_progress(f"跳过 {name}：账户配置已不存在")
                        continue
                    self.db.reconcile_cookie_flag(account)
                    flag = self.db.get_account_flag(name) or {}
                    if flag.get("cookie_invalid") or not bool(account.ok):
                        self._increment(skipped=1)
                        self._append_progress(f"跳过 {name}：Cookie 无效")
                        continue

                    self.db.update_production_loop_state(
                        current_account=name, current_job_id=""
                    )
                    self._increment(submitted=1)
                    self._append_progress(f"提交 {name}：interface=legacy count=1 threads=1")
                    try:
                        job = self.submitter(
                            account,
                            interface="legacy",
                            count=1,
                            threads=1,
                            settings=self.settings_loader(),
                            db=self.db,
                        )
                    except HMECreateRateLimitError as exc:
                        self._increment(failed=1)
                        next_wait = self._min_wait(next_wait, exc.retry_after_sec)
                        self._append_progress(
                            f"限流 {name}：retry_after={exc.retry_after_sec}s，本轮继续"
                        )
                        continue
                    except CookieInvalidError as exc:
                        self._increment(failed=1)
                        self._append_progress(f"跳过 {name}：{exc}")
                        continue
                    except Exception as exc:
                        self._increment(failed=1)
                        self._append_progress(
                            f"提交失败 {name}：{type(exc).__name__}: {exc}"
                        )
                        continue

                    job_id = str(getattr(job, "job_id", "") or "")
                    self.db.update_production_loop_state(current_job_id=job_id)
                    self._append_progress(f"任务 {job_id} 已提交")
                    terminal = self._terminal_job(job_id)
                    result = terminal.get("result") or {}
                    created = int(result.get("created") or terminal.get("created") or 0)
                    errors = list(result.get("errors") or [])
                    error_text = str(terminal.get("error") or "")
                    if created:
                        self._increment(created=created)
                        hmes = [str(item.get("hme") or "") for item in result.get("items") or []]
                        self._append_progress(
                            f"完成 {name}：created={created} {' '.join(h for h in hmes if h)}"
                        )
                    if terminal.get("status") != "done" or errors or error_text:
                        self._increment(failed=1)
                        detail = error_text or "; ".join(str(item) for item in errors) or "未知错误"
                        self._append_progress(f"任务失败 {name}：{detail}")
                        if "RATE_LIMIT" in detail or "rate_limited" in detail.lower():
                            quota = self.db.get_create_quota(name)
                            next_wait = self._min_wait(next_wait, quota.retry_after_sec)

                self.db.update_production_loop_state(
                    current_account="", current_job_id=""
                )
                if self._shutdown_event.is_set():
                    return
                state = self.db.get_production_loop_state()
                if self._stop_event.is_set() or not state.get("enabled"):
                    finish_status = "stopped"
                    break
                if self._deadline_hit(state):
                    finish_status = "completed"
                    break

                interval = max(1, int(state.get("interval_sec") or 2))
                wait_sec = max(interval, next_wait)
                deadline = int(state.get("deadline_at") or 0)
                if deadline:
                    wait_sec = min(wait_sec, max(0, deadline - self.clock()))
                if wait_sec <= 0:
                    finish_status = "completed"
                    break
                self.db.update_production_loop_state(next_run_at=self.clock() + wait_sec)
                if next_wait > interval:
                    self._append_progress(f"本轮最短风控 {next_wait}s，扫完后等待")
                if self._wait(wait_sec):
                    continue
                if self._shutdown_event.is_set():
                    return
                finish_status = "stopped" if self._stop_event.is_set() else "completed"
                break
        except Exception as exc:
            finish_status = "error"
            self.db.update_production_loop_state(last_error=f"{type(exc).__name__}: {exc}")
            self._append_progress(f"轮询线程异常：{type(exc).__name__}: {exc}")
        finally:
            if not self._shutdown_event.is_set():
                self.db.update_production_loop_state(
                    enabled=False,
                    status=finish_status,
                    current_account="",
                    current_job_id="",
                    next_run_at=0,
                )
                self._append_progress(f"轮询已结束：{finish_status}")

    def _wait(self, seconds: int) -> bool:
        end = self.clock() + max(0, int(seconds))
        while self.clock() < end:
            if self._shutdown_event.is_set() or self._stop_event.is_set():
                return False
            if self._deadline_hit():
                return False
            time.sleep(min(1.0, max(0.05, end - self.clock())))
        return True


_CONTROLLER: ProductionLoopController | None = None
_CONTROLLER_LOCK = threading.Lock()


def get_production_loop_controller() -> ProductionLoopController:
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = ProductionLoopController(
                get_db(),
                accounts_loader=get_accounts,
                settings_loader=get_settings,
            )
        return _CONTROLLER


__all__ = ["ProductionLoopController", "get_production_loop_controller"]
