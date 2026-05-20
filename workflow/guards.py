"""
workflow/guards.py — FSM Transition Guard Conditions
=====================================================

Guard functions are passed to django-fsm @transition decorators.
Each returns True if the transition is allowed, False otherwise.
Guards enforce business rules at the MODEL layer — before any side effects fire.

If a guard returns False, django-fsm raises TransitionNotAllowed,
which the exception handler maps to HTTP 400 TRANSITION_NOT_ALLOWED.

Reference: NBES System Architecture §3.1 — django-fsm guard conditions
"""


# ── Item Bank Guards ──────────────────────────────────────────────────────────


def has_mandatory_metadata(instance):
    """
    Prevents submission until all mandatory metadata fields are completed.
    Called before transition: Draft -> Submitted (or Revised -> Submitted)
    """
    mandatory_fields = [
        "blueprint_ref",
        "subject",
        "topic",
        "difficulty",
        "cognitive_level",
        "marks",
        "time",
        "source",
    ]

    for field in mandatory_fields:
        value = getattr(instance, field, None)
        # If the value is None, an empty string, or 0, it fails the check
        if value in [None, "", 0]:
            return False

    return True


def has_valid_mcq_config(instance) -> bool:
    """
    MCQ items must have exactly 4 options with exactly 1 correct answer.
    Non-MCQ items always pass this guard.
    TODO: Implement MCQ option validation.
    """
    # Item model uses `item_type` field.
    if getattr(instance, "item_type", None) != "mcq":
        return True
    # Try to validate the latest version content. We expect the front-end to
    # serialize MCQ options into a JSON structure with an `options` list where
    # each option may include an `is_correct` boolean. If the structure cannot
    # be parsed, fail the guard to enforce SRS correctness rather than allow an
    # underspecified MCQ into the review pipeline.
    try:
        import json

        latest = getattr(instance, "versions", None)
        if not latest:
            return False
        last_version = instance.versions.order_by("-version_no").first()
        if not last_version or not last_version.content:
            return False

        payload = json.loads(last_version.content)
        options = payload.get("options")
        if not isinstance(options, list) or len(options) < 2:
            return False

        # Count options marked as correct. Support common keys.
        correct_count = 0
        for opt in options:
            if isinstance(opt, dict) and (
                opt.get("is_correct") is True or opt.get("correct") is True
            ):
                correct_count += 1

        return correct_count == 1
    except Exception:
        # If parsing fails, treat as invalid to force authors to provide a
        # structured MCQ payload that the system can verify.
        return False


def has_reviewer_assigned(instance) -> bool:
    """Item must have a reviewer assigned before moving to in_review."""
    return bool(getattr(instance, "reviewer_id", None))


def has_sufficient_panel_votes(instance) -> bool:
    """
    Moderation panel requires 2 of 3 votes to reach a decision.
    TODO: Count votes from ItemPanelVote records.
    """
    # TODO: from apps.itembank.models import ItemPanelVote
    # approve_count = ItemPanelVote.objects.filter(item=instance, vote="approve").count()
    # return approve_count >= 2
    return True


def no_active_conflict(instance) -> bool:
    """
    Panellist must not have an active conflict-of-interest declaration
    against this item's subject.
    TODO: Check ConflictDeclaration records.
    """
    return True


def is_moderation_panel_member(instance) -> bool:
    """
    Only NBEC moderation panel members can send an item to the moderation panel.
    TODO: Check actor role from thread-local request.
    """
    return True


# ── Registration Guards ───────────────────────────────────────────────────────


def nlems_eligibility_verified(instance) -> bool:
    """
    Registration can only move to pending_payment after NLEMS has confirmed
    LLB + LPT eligibility. Checks candidate.eligibility_status (not Registration).
    """
    candidate = getattr(instance, "candidate", None)
    if candidate is None:
        return False
    return getattr(candidate, "eligibility_status", "") == "eligible"


def payment_confirmed(instance) -> bool:
    """
    Registration can only be confirmed after System 20 payment webhook
    has set payment_confirmed=True on the record.
    """
    return getattr(instance, "payment_confirmed", False)


# ── Marking Guards ────────────────────────────────────────────────────────────


def ai_scoring_complete(instance) -> bool:
    """AI scoring task has set ai_mark on the MarkingDecision."""
    decision = getattr(instance, "marking_decision", None)
    return decision is not None and decision.ai_mark is not None


def is_borderline(instance) -> bool:
    """
    Script AI mark is within ±5% (configurable) of the pass mark.
    TODO: Read pass mark from Sitting/Blueprint configuration.
    """
    return getattr(instance, "borderline_flagged", False)


def no_moderator_conflict(instance) -> bool:
    """
    Assigned moderator must not have a conflict declaration against this
    script's subject paper.
    TODO: Check ConflictDeclaration for moderator_id + paper.
    """
    return True


def has_justification(instance) -> bool:
    """
    Moderator must provide a justification of ≥30 words if they adjusted
    the AI mark.
    TODO: Check MarkingDecision.justification word count.
    """
    decision = getattr(instance, "marking_decision", None)
    if decision is None:
        return True
    if decision.moderator_mark is None:
        return True
    justification = getattr(decision, "justification", "") or ""
    return len(justification.split()) >= 30


def reconciliation_required(instance) -> bool:
    """
    Reconciliation is required when AI mark and moderator mark differ
    beyond the configured threshold.
    TODO: Read threshold from SittingConfig.
    """
    return getattr(instance, "reconciliation_required", False)


# ── Results Guards ────────────────────────────────────────────────────────────


def normalisation_complete(instance) -> bool:
    """All scripts in the sitting have a final_mark_locked status."""
    return getattr(instance, "normalisation_complete", False)


def dg_signoff_recorded(instance) -> bool:
    """Director General sign-off has been recorded on the ResultSet."""
    return bool(getattr(instance, "dg_signoff_ref", None))


# ── Re-sit Guards ─────────────────────────────────────────────────────────────


def resit_fee_confirmed(instance) -> bool:
    """System 20 payment webhook confirmed re-sit fee."""
    return getattr(instance, "fee_confirmed", False)


def below_attempt_limit(instance) -> bool:
    """
    Candidate has not exceeded the §73 maximum attempt limit
    (including any NBEC exception grants).
    TODO: Check AttemptCounter for this candidate + paper.
    """
    return True
