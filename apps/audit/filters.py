"""apps/audit/filters.py — Query filters for AuditEvent search."""
from django.db.models import Q


def apply_audit_filters(queryset, params):
    """Apply search filters to an AuditEvent queryset from request.query_params."""
    actor_id = params.get("actor_id")
    action = params.get("action")
    entity_type = params.get("entity_type")
    entity_id = params.get("entity_id")
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    source_system = params.get("source_system")
    q = params.get("q")  # free-text on action field

    if actor_id:
        queryset = queryset.filter(actor_id=actor_id)
    if action:
        queryset = queryset.filter(action__iexact=action)
    if entity_type:
        queryset = queryset.filter(entity_type__iexact=entity_type)
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)
    if source_system:
        queryset = queryset.filter(source_system__iexact=source_system)
    if q:
        queryset = queryset.filter(Q(action__icontains=q) | Q(entity_type__icontains=q))

    return queryset
