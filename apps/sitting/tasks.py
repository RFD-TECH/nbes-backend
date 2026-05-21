"""apps/sitting/tasks.py — Phase 4 Celery tasks.

Two scheduled jobs:

* :func:`monitor_t30_lock` — runs daily, auto-locks any CONFIGURED+approved
  sitting whose ``sitting_date`` is ≤ 30 calendar days away. Idempotent: a
  second run on an already-locked sitting is a no-op (handled in
  :func:`apps.sitting.services.lock_sitting`).

* :func:`send_t30_reminders` — runs daily, dispatches T-45 / T-35 / T-31
  reminder notifications to NBEC members for each upcoming sitting. The
  notification dispatcher itself isn't wired yet (System 21 not ready) so we
  record an AuditEvent + publish a domain event and rely on whatever
  notification consumer is added later. Idempotent via a per-sitting marker
  on :class:`Sitting` (reminder fingerprint stored on the lock-event audit
  trail — see ``_already_sent``).

Both tasks are deliberately fault-tolerant: one bad sitting must not block
the rest of the run.

Beat scheduling is *not* wired in this PR (see ``config/celery.py``). The
expected entries::

    "sitting:t30-lock":      {"task": "apps.sitting.tasks.monitor_t30_lock",
                              "schedule": crontab(minute=5, hour=0)},
    "sitting:t30-reminders": {"task": "apps.sitting.tasks.send_t30_reminders",
                              "schedule": crontab(minute=15, hour=8)},
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from celery import shared_task
from django.utils import timezone

from apps.audit.models import AuditEvent
from shared.events import publish

from . import events as ev
from .models import Sitting, SittingLockEvent

logger = logging.getLogger(__name__)


# ── T-30 auto-lock ────────────────────────────────────────────────────────-


T30_LOCK_OFFSET_DAYS = 30
REMINDER_OFFSETS_DAYS = (45, 35, 31)  # T-45, T-35, T-31


@shared_task(
    name="apps.sitting.tasks.monitor_t30_lock",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def monitor_t30_lock(self) -> dict:
    """Auto-lock every approved sitting that has reached T-30.

    Returns a summary dict (used by tests and by ``celery inspect`` output).
    Errors on individual sittings are caught so a single bad row can't block
    the rest of the run; the failure is recorded on the audit trail.
    """
    from .services import lock_sitting, SittingValidationError

    today = timezone.localdate()
    cutoff = today + timedelta(days=T30_LOCK_OFFSET_DAYS)

    candidates = (
        Sitting.objects
        .filter(status=Sitting.Status.CONFIGURED)
        .filter(approved_at__isnull=False)
        .filter(sitting_date__lte=cutoff)
        .order_by("sitting_date")
    )

    locked, errored = [], []
    for sitting in candidates:
        try:
            lock_sitting(
                actor_id=None,
                sitting=sitting,
                kind=SittingLockEvent.Kind.AUTO_LOCK,
                justification=(
                    f"Automatic T-30 lock — sitting_date {sitting.sitting_date.isoformat()} "
                    f"is within {T30_LOCK_OFFSET_DAYS} days of {today.isoformat()}."
                ),
            )
            locked.append(sitting.ref)
        except SittingValidationError as exc:
            errored.append({"ref": sitting.ref, "code": exc.code, "message": exc.message})
            logger.warning(
                "T-30 auto-lock skipped sitting=%s code=%s reason=%s",
                sitting.ref, exc.code, exc.message,
            )
            AuditEvent.record(
                actor_id=None,
                action="SITTING_T30_LOCK_SKIPPED",
                entity_type="sitting",
                entity_id=sitting.ref,
                new_state={"code": exc.code, "message": exc.message},
            )
        except Exception as exc:  # noqa: BLE001 — task-level catch
            errored.append({"ref": sitting.ref, "code": "UNEXPECTED", "message": str(exc)})
            logger.exception("T-30 auto-lock failed for sitting=%s", sitting.ref)

    summary = {
        "ran_at": timezone.now().isoformat(),
        "today": today.isoformat(),
        "cutoff": cutoff.isoformat(),
        "locked": locked,
        "errored": errored,
        "checked": len(locked) + len(errored),
    }
    return summary


# ── T-45 / T-35 / T-31 reminders ─────────────────────────────────────────-


@shared_task(
    name="apps.sitting.tasks.send_t30_reminders",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def send_t30_reminders(self) -> dict:
    """Dispatch T-45 / T-35 / T-31 reminders for upcoming sittings.

    Idempotent: ``_already_sent`` checks whether we've already fired a
    reminder of this offset for this sitting, so a second run on the same
    day is a safe no-op.

    The actual notification send is delegated to whatever notification
    dispatcher is wired in later (Phase 1/9 hand-off). For now we audit-log
    and publish a ``T30ReminderDue`` outbox event so a downstream consumer
    can fan it out via System 21 once that lands.
    """
    today = timezone.localdate()
    sent: list[dict] = []
    skipped: list[dict] = []

    for offset in REMINDER_OFFSETS_DAYS:
        target_date = today + timedelta(days=offset)
        candidates = (
            Sitting.objects
            .filter(sitting_date=target_date)
            .exclude(status__in=[Sitting.Status.CLOSED])
        )
        for sitting in candidates:
            if _already_sent(sitting, offset, today):
                skipped.append({"ref": sitting.ref, "offset": offset, "reason": "already_sent"})
                continue
            _record_reminder(sitting, offset, today)
            sent.append({"ref": sitting.ref, "offset": offset})

    return {
        "ran_at": timezone.now().isoformat(),
        "today": today.isoformat(),
        "sent": sent,
        "skipped": skipped,
    }


def _already_sent(sitting: Sitting, offset: int, on_date: date) -> bool:
    """Have we already emitted a reminder at this offset for this sitting today?"""
    return AuditEvent.objects.filter(
        entity_type="sitting",
        entity_id=sitting.ref,
        action=ev.T30_REMINDER_SENT,
        new_state__contains={"offset_days": offset, "date": on_date.isoformat()},
    ).exists()


def _record_reminder(sitting: Sitting, offset: int, on_date: date) -> None:
    AuditEvent.record(
        actor_id=None,
        action=ev.T30_REMINDER_SENT,
        entity_type="sitting",
        entity_id=sitting.ref,
        new_state={
            "offset_days": offset,
            "date": on_date.isoformat(),
            "sitting_date": sitting.sitting_date.isoformat(),
        },
    )
    publish(
        "T30ReminderDue",
        {
            "ref": sitting.ref,
            "offset_days": offset,
            "sitting_date": sitting.sitting_date.isoformat(),
            "fires_on": on_date.isoformat(),
        },
    )
