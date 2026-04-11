"""Background maintenance scheduler for NCMS.

Periodically runs consolidation, dream cycles, episode closure, and decay
passes as asyncio background tasks. Each loop respects its feature flag
and interval configuration. The maintenance_enabled config flag is the
master switch — nothing runs unless it is True.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ncms.application.consolidation_service import ConsolidationService
    from ncms.application.episode_service import EpisodeService
    from ncms.config import NCMSConfig

logger = logging.getLogger(__name__)


@dataclass
class TaskStatus:
    """Status snapshot for a single maintenance task."""

    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_duration_ms: float = 0.0
    run_count: int = 0
    error_count: int = 0
    last_error: str | None = None


@dataclass
class SchedulerStatus:
    """Overall scheduler status."""

    running: bool = False
    tasks: dict[str, TaskStatus] = field(default_factory=dict)


class MaintenanceScheduler:
    """Background asyncio scheduler for periodic maintenance operations.

    Each loop sleeps for its configured interval, then calls the
    corresponding service method. Errors are caught and logged —
    a single failure never kills the loop.

    Args:
        consolidation_svc: Provides run_consolidation_pass, run_decay_pass,
            and run_dream_cycle.
        episode_svc: Provides close_stale_episodes (optional).
        config: NCMSConfig with maintenance_* and feature-flag fields.
        event_log: Optional event log for observability.
    """

    _TASK_DEFS: list[tuple[str, str, str, str]] = [
        # (task_name, interval_config_attr, feature_flag_attr, service_method)
        (
            "consolidation",
            "maintenance_consolidation_interval_minutes",
            "consolidation_knowledge_enabled",
            "run_consolidation_pass",
        ),
        (
            "dream",
            "maintenance_dream_interval_minutes",
            "dream_cycle_enabled",
            "run_dream_cycle",
        ),
        (
            "episode_close",
            "maintenance_episode_close_interval_minutes",
            "episodes_enabled",
            "close_stale_episodes",
        ),
        (
            "decay",
            "maintenance_decay_interval_minutes",
            "_always",  # decay has no separate feature flag
            "run_decay_pass",
        ),
    ]

    def __init__(
        self,
        consolidation_svc: ConsolidationService,
        episode_svc: EpisodeService | None,
        config: NCMSConfig,
        event_log: Any = None,
    ) -> None:
        self._consolidation_svc = consolidation_svc
        self._episode_svc = episode_svc
        self._config = config
        self._event_log = event_log

        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._status: dict[str, TaskStatus] = {}
        self._running = False

    # ── Public API ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create background asyncio tasks for each enabled maintenance loop."""
        if not self._config.maintenance_enabled:
            logger.info("Maintenance scheduler disabled (maintenance_enabled=false)")
            return

        self._running = True

        for task_name, interval_attr, flag_attr, method_name in self._TASK_DEFS:
            # Check feature flag
            if flag_attr != "_always" and not getattr(self._config, flag_attr, False):
                logger.debug(
                    "Maintenance task '%s' skipped (%s=false)", task_name, flag_attr
                )
                continue

            # episode_close needs episode_svc
            if task_name == "episode_close" and self._episode_svc is None:
                logger.debug(
                    "Maintenance task 'episode_close' skipped (no episode_svc)"
                )
                continue

            interval_minutes: int = getattr(self._config, interval_attr)
            self._status[task_name] = TaskStatus()
            self._tasks[task_name] = asyncio.create_task(
                self._loop(task_name, interval_minutes, method_name),
                name=f"maintenance-{task_name}",
            )

        if self._tasks:
            names = ", ".join(self._tasks.keys())
            logger.info("Maintenance scheduler started: %s", names)
        else:
            logger.info("Maintenance scheduler: no tasks enabled")

    async def stop(self) -> None:
        """Cancel all running maintenance tasks."""
        self._running = False
        import contextlib

        for name, task in self._tasks.items():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.debug("Maintenance task '%s' stopped", name)
        self._tasks.clear()
        logger.info("Maintenance scheduler stopped")

    def status(self) -> SchedulerStatus:
        """Return current scheduler status snapshot."""
        return SchedulerStatus(
            running=self._running,
            tasks=dict(self._status),
        )

    async def run_now(self, task_name: str) -> dict[str, Any]:
        """Manually trigger a maintenance task by name.

        Args:
            task_name: One of "consolidation", "dream", "episode_close",
                "decay", or "all".

        Returns:
            Dict with task results. For "all", a dict mapping each task
            name to its result.
        """
        if task_name == "all":
            results: dict[str, Any] = {}
            for name, _, _, method_name in self._TASK_DEFS:
                if name == "episode_close" and self._episode_svc is None:
                    continue
                results[name] = await self._execute_task(name, method_name)
            return results

        # Find the matching task definition
        for name, _, _, method_name in self._TASK_DEFS:
            if name == task_name:
                if name == "episode_close" and self._episode_svc is None:
                    return {"error": "episode_svc not available"}
                result = await self._execute_task(name, method_name)
                return {name: result}

        return {"error": f"unknown task: {task_name}"}

    # ── Internal ──────────────────────────────────────────────────────

    async def _loop(
        self,
        task_name: str,
        interval_minutes: int,
        method_name: str,
    ) -> None:
        """Background loop: sleep → execute → repeat."""
        interval_seconds = interval_minutes * 60
        status = self._status[task_name]
        status.next_run_at = datetime.now(UTC)

        while self._running:
            # Sleep first — let the system warm up before first maintenance
            try:
                status.next_run_at = datetime.now(UTC)
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                return

            await self._execute_task(task_name, method_name)

    async def _execute_task(
        self,
        task_name: str,
        method_name: str,
    ) -> Any:
        """Run a single maintenance task, updating status and emitting events."""
        if task_name not in self._status:
            self._status[task_name] = TaskStatus()
        status = self._status[task_name]

        t0 = time.monotonic()
        result: Any = None
        try:
            svc = (
                self._episode_svc
                if task_name == "episode_close"
                else self._consolidation_svc
            )
            method = getattr(svc, method_name)
            result = await method()

            elapsed_ms = (time.monotonic() - t0) * 1000
            status.last_run_at = datetime.now(UTC)
            status.last_duration_ms = elapsed_ms
            status.run_count += 1
            status.last_error = None

            logger.info(
                "Maintenance task '%s' completed in %.0fms: %s",
                task_name,
                elapsed_ms,
                result,
            )

            self._emit_event(task_name, elapsed_ms, result=result)

        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            status.last_run_at = datetime.now(UTC)
            status.last_duration_ms = elapsed_ms
            status.run_count += 1
            status.error_count += 1
            status.last_error = str(exc)

            logger.exception(
                "Maintenance task '%s' failed after %.0fms", task_name, elapsed_ms
            )

            self._emit_event(task_name, elapsed_ms, error=str(exc))

        return result

    def _emit_event(
        self,
        task_name: str,
        duration_ms: float,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """Emit a dashboard event if event_log is available."""
        if self._event_log is None:
            return

        data: dict[str, Any] = {
            "task": task_name,
            "duration_ms": round(duration_ms, 2),
        }
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error

        try:
            from ncms.infrastructure.observability.event_log import DashboardEvent

            event_type = (
                "maintenance.task_error" if error else "maintenance.task_complete"
            )
            self._event_log.emit(DashboardEvent(
                type=event_type,
                data=data,
            ))
        except Exception:
            pass  # event log is best-effort
