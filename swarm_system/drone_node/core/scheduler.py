"""
core/scheduler.py
-----------------
APScheduler-based mission scheduler for this swarm system.

The ``MissionScheduler`` wraps an ``AsyncIOScheduler`` and is responsible
for converting a ``MissionPlan``'s ``ScheduleConfig`` into concrete APScheduler
jobs that fire the mission runner's ``run()`` at the right moment(s).

Scheduling logic
~~~~~~~~~~~~~~~~
1. Compute the wall-clock delta between *now* and ``plan.schedule.start_date``.
2. If the start_date is in the past (delta ≤ 0): log a WARNING and submit a
   ``DateTrigger`` job scheduled 2 seconds into the future so the event loop
   has time to start before the mission fires.
3. Otherwise: log "Mission ALPHA launches in X hours Y minutes at <datetime>"
   and schedule a ``DateTrigger`` job at the exact start_date.
4. If ``plan.schedule.repeat_cron`` is set: additionally schedule a
   ``CronTrigger`` job for recurring launches.

Usage
-----
>>> scheduler = MissionScheduler(coordinator)
>>> scheduler.schedule_mission(plan)
>>> scheduler.start()
>>> asyncio.get_event_loop().run_forever()
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from core.mission_plan import MissionPlan

log = logging.getLogger(__name__)

# Seconds ahead to schedule a "run immediately" job when start_date is in past
_IMMEDIATE_OFFSET_SEC = 2


class MissionScheduler:
    """
    Wraps APScheduler's ``AsyncIOScheduler`` to drive mission execution.

    Parameters
    ----------
    coordinator : A ``SwarmCoordinator`` instance (or any object with an
                  async ``run()`` coroutine method).

    Attributes
    ----------
    _scheduler   : The underlying ``AsyncIOScheduler`` instance.
    _coordinator : Reference to the swarm coordinator.
    """

    def __init__(self, coordinator: object) -> None:
        """
        Initialise the scheduler with a coordinator reference.

        Parameters
        ----------
        coordinator : Object providing an async ``run()`` method that
                      executes the full swarm mission.
        """
        self._coordinator = coordinator
        self._scheduler   = AsyncIOScheduler()
        log.debug("MissionScheduler initialised.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def schedule_mission(self, plan: MissionPlan) -> None:
        """
        Register APScheduler jobs derived from *plan*'s schedule config.

        Behaviour
        ---------
        * If ``plan.schedule.start_date`` is in the past: logs a WARNING
          and creates a ``DateTrigger`` job %d seconds from now.
        * Otherwise: logs an informational countdown and creates a
          ``DateTrigger`` job at the precise start_date.
        * If ``plan.schedule.repeat_cron`` is non-None: additionally
          registers a ``CronTrigger`` job for recurring execution.

        Parameters
        ----------
        plan : The fully parsed mission plan to schedule.
        """
        now        = datetime.now()
        start_date = plan.schedule.start_date

        # Strip tz info for comparison when start_date is naive
        if start_date.tzinfo is not None:
            now = datetime.now(tz=start_date.tzinfo)

        delta: timedelta = start_date - now

        if delta.total_seconds() <= 0:
            fire_at = datetime.now() + timedelta(seconds=_IMMEDIATE_OFFSET_SEC)
            log.warning(
                "Mission '%s' start_date %s is in the past. "
                "Running immediately (in %d s).",
                plan.name, start_date.isoformat(), _IMMEDIATE_OFFSET_SEC,
            )
        else:
            fire_at = start_date
            total_seconds = int(delta.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            log.info(
                "Mission '%s' launches in %dh %dm %ds at %s.",
                plan.name, hours, minutes, seconds,
                start_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
            )

        # One-shot launch job
        self._scheduler.add_job(
            func=self._run_coordinator,
            trigger=DateTrigger(run_date=fire_at),
            id=f"mission_{plan.id}_launch",
            name=f"Launch: {plan.name}",
            replace_existing=True,
        )
        log.info(
            "Scheduled one-shot job 'mission_%s_launch' at %s.",
            plan.id, fire_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # Optional recurring cron job
        if plan.schedule.repeat_cron:
            self._add_cron_job(plan)

    def start(self) -> None:
        """
        Start the underlying ``AsyncIOScheduler``.

        Must be called *after* ``schedule_mission()`` and *before* the
        event loop enters ``run_forever()``.
        """
        log.info("Starting MissionScheduler.")
        self._scheduler.start()

    def stop(self) -> None:
        """
        Gracefully shut down the ``AsyncIOScheduler``.

        Safe to call even if ``start()`` has not yet been called.
        """
        log.info("Stopping MissionScheduler.")
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("Exception during scheduler shutdown: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_cron_job(self, plan: MissionPlan) -> None:
        """
        Register a ``CronTrigger`` repeat job for *plan*.

        Parameters
        ----------
        plan : Mission plan whose ``schedule.repeat_cron`` is non-None.
        """
        cron_expr: str = plan.schedule.repeat_cron  # type: ignore[assignment]
        fields = cron_expr.split()
        if len(fields) != 5:
            log.error(
                "repeat_cron '%s' for mission '%s' is not a valid 5-field "
                "cron expression. Skipping recurring schedule.",
                cron_expr, plan.name,
            )
            return

        minute, hour, day, month, day_of_week = fields
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=plan.schedule.timezone,
        )
        self._scheduler.add_job(
            func=self._run_coordinator,
            trigger=trigger,
            id=f"mission_{plan.id}_repeat",
            name=f"Repeat: {plan.name}",
            replace_existing=True,
        )
        log.info(
            "Scheduled recurring job 'mission_%s_repeat' with cron '%s' (tz=%s).",
            plan.id, cron_expr, plan.schedule.timezone,
        )

    async def _run_coordinator(self) -> None:
        """
        Async callable submitted to APScheduler.

        Delegates to ``coordinator.run()`` and logs any unhandled exception
        so APScheduler does not silently swallow failures.
        """
        log.info("MissionScheduler firing: calling coordinator.run().")
        try:
            await self._coordinator.run()  # type: ignore[attr-defined]
        except Exception as exc:
            log.exception("Unhandled exception during coordinator.run(): %s", exc)

