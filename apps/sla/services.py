"""apps/sla/services.py - SLA status computation."""
from django.utils import timezone

def compute_sla_status(instance) -> str:
    now = timezone.now()
    threshold = timezone.timedelta(hours=float(instance.config.at_risk_threshold_hours))
    if now >= instance.deadline:
        return "overdue"
    elif now >= (instance.deadline - threshold):
        return "at_risk"
    return "on_track"

def fire_escalation(instance):
    from apps.sla.models import SLAEscalation
    SLAEscalation.objects.create(sla_instance=instance, trigger_status=instance.status, escalated_to=[])