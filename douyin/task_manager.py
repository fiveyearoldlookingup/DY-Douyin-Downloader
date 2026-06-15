"""多任务管理器。

支持同时运行多个爬取/直播任务，替代旧的单任务模型。

用法:
    from douyin.task_manager import TaskManager, TaskType, TaskStatus

    manager = TaskManager(max_workers=2)
    task = manager.submit(TaskType.CRAWL, target_id, crawl_fn, target=target_id)
    manager.list_tasks()
    manager.cancel(task.id)
"""

import concurrent.futures
import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════

class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, enum.Enum):
    CRAWL = "crawl"
    LIVE = "live"
    SYNC = "sync"


# ═══════════════════════════════════════════════════════════════
# Task 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: TaskType = TaskType.CRAWL
    target: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0.0 ~ 1.0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "target": self.target,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
        }


# ═══════════════════════════════════════════════════════════════
# TaskManager
# ═══════════════════════════════════════════════════════════════

class TaskManager:
    """多任务并发管理器。

    特性:
    - 线程池执行，默认 2 个并发工作线程
    - 任务支持取消（通过 threading.Event）
    - 事件回调通知（供 SSE 使用）
    - 任务历史保留（仅本进程生命周期）
    """

    def __init__(self, max_workers: int = 2):
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dy-task-",
        )
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._event_callbacks: list[Callable[[dict], None]] = []

    # ── 公开 API ──────────────────────────────────────────────

    def submit(
        self,
        task_type: TaskType,
        target: str,
        fn: Callable,
        **fn_kwargs,
    ) -> Task:
        """提交新任务。

        Args:
            task_type: 任务类型 (crawl/live/sync)
            target: 任务目标 URL/ID
            fn: 要执行的可调用对象 fn(task=task, **kwargs)
            **fn_kwargs: 传递给 fn 的额外参数

        Returns:
            Task 对象（包含任务 ID）
        """
        task = Task(type=task_type, target=target)
        with self._lock:
            self._tasks[task.id] = task

        future = self._executor.submit(self._run_task, task, fn, fn_kwargs)
        future.add_done_callback(lambda f: self._on_future_done(task, f))
        return task

    def cancel(self, task_id: str) -> bool:
        """取消任务。返回是否成功。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                task.cancel_event.set()
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                self._notify(task)
                return True
        return False

    def list_tasks(
        self, status: TaskStatus | None = None
    ) -> list[dict]:
        """列出任务，可按状态过滤。"""
        with self._lock:
            tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        # 按创建时间倒序
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in tasks]

    def get_task(self, task_id: str) -> dict | None:
        """获取单个任务信息。"""
        with self._lock:
            task = self._tasks.get(task_id)
        return task.to_dict() if task else None

    def subscribe_events(self, callback: Callable[[dict], None]) -> None:
        """订阅任务状态变更事件。callback 接收 task.to_dict()。"""
        self._event_callbacks.append(callback)

    def unsubscribe_events(self, callback: Callable[[dict], None]) -> None:
        """取消订阅。"""
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def shutdown(self, wait: bool = True) -> None:
        """关闭任务管理器。"""
        # 取消所有运行中的任务
        with self._lock:
            for task in self._tasks.values():
                if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                    task.cancel_event.set()
        self._executor.shutdown(wait=wait)

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    @property
    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == TaskStatus.QUEUED)

    # ── 内部 ──────────────────────────────────────────────────

    def _run_task(self, task: Task, fn: Callable, kwargs: dict) -> None:
        """在线程池中执行任务。"""
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._notify(task)

        try:
            result = fn(task=task, **kwargs)
            if task.cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
            else:
                task.status = TaskStatus.COMPLETED
                task.result = result if isinstance(result, dict) else {}
                task.progress = 1.0
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.message = str(e)
        finally:
            task.finished_at = time.time()
            self._notify(task)

    def _on_future_done(self, task: Task, future: concurrent.futures.Future) -> None:
        """Future 完成回调（处理未捕获的异常）。"""
        if not future.cancelled():
            try:
                future.result()
            except Exception as e:
                if task.status not in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                    task.status = TaskStatus.FAILED
                    task.message = str(e)
                    task.finished_at = time.time()
                    self._notify(task)

    def _notify(self, task: Task) -> None:
        """通知所有订阅者。"""
        data = task.to_dict()
        for cb in self._event_callbacks:
            try:
                cb(data)
            except Exception:
                pass
