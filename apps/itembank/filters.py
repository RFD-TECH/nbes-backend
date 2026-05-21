"""Filter sets for the itembank app (SRS-NBE-F02-10).

Supported filters:

* ``subject``, ``topic``, ``difficulty``, ``cognitive_level``,
  ``status``, ``blueprint_ref``, ``item_type`` — direct equality.
* ``author`` — UUID of the authoring user (Item.author_id).
* ``used_in_sitting`` — restricts to items that appeared in the given
  sitting_ref via ``ItemUsage``.
* ``quality_flagged`` — analytics flag.
* ``marks_min`` / ``marks_max`` — numeric range over ``Item.marks``.
* ``not_in_recent_sittings`` — excludes any item in the cool-down window.
* ``or_mode=true`` — switches AND composition to OR over the simple
  string filters (subject/topic/difficulty/cognitive_level/blueprint_ref/
  status/item_type). Range and boolean filters are still ANDed in.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q
from django_filters import rest_framework as filters

from .models import Item, ItemUsage


class ItemFilter(filters.FilterSet):
    """Filter set powering the item search endpoint."""

    OR_FIELDS = (
        "subject",
        "topic",
        "difficulty",
        "cognitive_level",
        "status",
        "blueprint_ref",
        "item_type",
    )

    subject = filters.CharFilter(method="filter_subject")
    topic = filters.CharFilter(method="filter_topic")
    difficulty = filters.CharFilter(method="filter_difficulty")
    cognitive_level = filters.CharFilter(method="filter_cognitive_level")
    status = filters.CharFilter(method="filter_status")
    blueprint_ref = filters.CharFilter(method="filter_blueprint_ref")
    item_type = filters.CharFilter(method="filter_item_type")
    quality_flagged = filters.BooleanFilter(field_name="quality_flagged")
    author = filters.UUIDFilter(field_name="author_id__id")
    used_in_sitting = filters.CharFilter(method="filter_used_in_sitting")

    marks_min = filters.NumberFilter(field_name="marks", lookup_expr="gte")
    marks_max = filters.NumberFilter(field_name="marks", lookup_expr="lte")

    not_in_recent_sittings = filters.BooleanFilter(method="filter_cool_down")
    or_mode = filters.BooleanFilter(method="filter_or_mode_marker")

    class Meta:
        model = Item
        fields = [
            "subject",
            "topic",
            "difficulty",
            "cognitive_level",
            "status",
            "blueprint_ref",
            "item_type",
            "quality_flagged",
            "author",
        ]

    # ------------------------------------------------------------------ helpers
    def _is_or_mode(self) -> bool:
        raw = (self.data or {}).get("or_mode", "false")
        return str(raw).lower() in ("1", "true", "yes")

    def _apply_eq(self, qs, field: str, value: str):
        if value in (None, ""):
            return qs
        lookup = f"{field}__iexact" if field != "status" else field
        if self._is_or_mode():
            # OR-mode: collect the term into an internal Q bag, applied
            # once in ``qs`` so callers get an OR across all string filters.
            existing: Q = getattr(self, "_or_q", Q())
            existing |= Q(**{lookup: value})
            self._or_q = existing
            return qs
        return qs.filter(**{lookup: value})

    # -------------------------------------------------------------- per-field
    def filter_subject(self, qs, name, value):
        return self._apply_eq(qs, "subject", value)

    def filter_topic(self, qs, name, value):
        return self._apply_eq(qs, "topic", value)

    def filter_difficulty(self, qs, name, value):
        return self._apply_eq(qs, "difficulty", value)

    def filter_cognitive_level(self, qs, name, value):
        return self._apply_eq(qs, "cognitive_level", value)

    def filter_status(self, qs, name, value):
        return self._apply_eq(qs, "status", value)

    def filter_blueprint_ref(self, qs, name, value):
        return self._apply_eq(qs, "blueprint_ref", value)

    def filter_item_type(self, qs, name, value):
        return self._apply_eq(qs, "item_type", value)

    def filter_used_in_sitting(self, qs, name, value):
        if not value:
            return qs
        used_ids = ItemUsage.objects.filter(sitting_ref=value).values_list(
            "item_id", flat=True
        )
        return qs.filter(id__in=used_ids)

    def filter_or_mode_marker(self, qs, name, value):
        # No-op: serves only to surface the parameter in DRF's filter UI
        # and the OpenAPI schema. Actual OR composition happens in ``qs``.
        return qs

    def filter_cool_down(self, qs, name, value):
        if not value:
            return qs
        cool_down = getattr(settings, "ITEM_COOLDOWN_SITTINGS", 3)
        from django.db.models import Max  # local import to avoid top-level cost

        recent = list(
            ItemUsage.objects.values("sitting_ref")
            .annotate(latest=Max("recorded_at"))
            .order_by("-latest")
            .values_list("sitting_ref", flat=True)[:cool_down]
        )
        if not recent:
            return qs
        used = ItemUsage.objects.filter(sitting_ref__in=recent).values_list(
            "item_id", flat=True
        )
        return qs.exclude(id__in=used)

    # ----------------------------------------------------------- finalisation
    @property
    def qs(self):
        base_qs = super().qs
        or_q = getattr(self, "_or_q", None)
        if or_q is not None:
            base_qs = Item.objects.filter(or_q).filter(id__in=base_qs.values("id"))
        return base_qs.distinct()
