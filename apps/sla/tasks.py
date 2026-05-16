"""apps/sla/tasks.py - SLA monitor tasks."""
from celery import shared_task

@shared_task(name="apps.sla.tasks.check_all_slas", queue="sla-monitor")
def check_all_slas():
    from apps.sla.models import SLAInstance
    from apps.sla.services import compute_sla_status, fire_escalation
    from shared.events import publish
    for instance in SLAInstance.objects.filter(status__in=["on_track", "at_risk"]):
        new_status = compute_sla_status(instance)
        if new_status != instance.status:
            instance.status = new_status
            instance.save(update_fields=["status", "updated_at"])
            fire_escalation(instance)
            publish("SLA" + new_status.title().replace("_", ""), {"sla_instance_id": str(instance.id)})

@shared_task(name="apps.sla.tasks.check_cert_trigger_sla", queue="sla-monitor")
def check_cert_trigger_sla():
    from django.utils import timezone
    from apps.cert_trigger.models import CertTriggerRecord
    from shared.events import publish
    for record in CertTriggerRecord.objects.filter(status=CertTriggerRecord.Status.FIRED, sla_deadline__lt=timezone.now()):
        record.status = CertTriggerRecord.Status.SLA_BREACHED
        record.save(update_fields=["status"])
        publish("CertTriggerSLABreached", {"trigger_id": str(record.id)})