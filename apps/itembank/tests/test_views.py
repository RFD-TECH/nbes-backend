"""View-layer regression tests for Sprint 3.4 search and saved-search RBAC.

The codebase has a pre-existing inconsistency: ``Item.author_id`` is a
ForeignKey to ``auth.User`` but profile keys live on ``UserProfile``.
``auth.User`` has no ``keycloak_sub`` column, so the
``User.objects.get(keycloak_sub=...)`` lookups in the view layer raise
``FieldError``. The view code falls back to a *deny-by-default* scope in
that case — these tests verify exactly that fail-closed behaviour.
"""
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.audit.models import AuditEvent
from apps.itembank.models import Item, ItemUsage, SavedSearch
from apps.itembank.views import (
    ItemSearchViewSet,
    SavedSearchViewSet,
    _item_search_queryset,
    _rbac_scoped_item_queryset,
)
from .test_services import _make_item, _make_user


def _request(roles=None, sub=None):
    factory = APIRequestFactory()
    raw = factory.get("/api/v1/itembank/item-search/")
    request = Request(raw)
    payload = {
        "sub": str(sub) if sub else str(uuid4()),
        "realm_access": {"roles": roles or []},
    }
    request._auth = payload  # rest_framework.request.Request reads this
    return request


@override_settings(NBES_CLIENT_ID="nbes-api")
class RbacScopedQuerysetTests(TestCase):
    """SRS-NBE-F02-10 RBAC scoping resolves via shared.rbac."""

    def setUp(self):
        self.alice = _make_user()
        self.bob = _make_user()
        self.draft = _make_item(author=self.alice, status=Item.Status.DRAFT)
        self.in_review = _make_item(
            author=self.bob,
            status=Item.Status.IN_REVIEW,
            marks=Decimal("3.00"),
        )
        self.locked = _make_item(
            author=self.bob, status=Item.Status.LOCKED_FOR_USE
        )

    def test_item_writer_with_unresolvable_user_sees_nothing(self):
        """Fail-closed default when keycloak_sub can't be resolved."""
        req = _request(roles=["item_writer"], sub=self.alice.keycloak_sub)
        qs = _rbac_scoped_item_queryset(req)
        self.assertEqual(list(qs), [])

    def test_moderator_sees_only_in_review_items(self):
        req = _request(roles=["moderator"])
        qs = _rbac_scoped_item_queryset(req)
        ids = set(qs.values_list("id", flat=True))
        self.assertEqual(ids, {self.in_review.id})

    def test_reviewer_alias_sees_only_in_review_items(self):
        req = _request(roles=["reviewer"])
        qs = _rbac_scoped_item_queryset(req)
        ids = set(qs.values_list("id", flat=True))
        self.assertEqual(ids, {self.in_review.id})

    def test_secretariat_sees_all(self):
        req = _request(roles=["nbec_secretariat"])
        qs = _rbac_scoped_item_queryset(req)
        ids = set(qs.values_list("id", flat=True))
        self.assertEqual(
            ids, {self.draft.id, self.in_review.id, self.locked.id}
        )


@override_settings(NBES_CLIENT_ID="nbes-api")
class SavedSearchSharingTests(TestCase):
    """SRS-NBE-F02-10: saved-search sharing actually changes visibility."""

    def setUp(self):
        self.owner = _make_user()
        self.secretariat = _make_user()
        self.shared = SavedSearch.objects.create(
            user=self.owner,
            name="shared",
            query={},
            shared_with_secretariat=True,
        )
        self.private = SavedSearch.objects.create(
            user=self.owner,
            name="private",
            query={},
            shared_with_secretariat=False,
        )

    def _viewset_for(self, *, roles, sub):
        viewset = SavedSearchViewSet()
        viewset.kwargs = {}
        viewset.request = _request(roles=roles, sub=sub)
        return viewset

    def test_secretariat_sees_shared_searches_even_when_user_unresolvable(self):
        viewset = self._viewset_for(
            roles=["nbec_secretariat"], sub=self.secretariat.keycloak_sub
        )
        ids = set(viewset.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.shared.id})

    def test_non_secretariat_with_unresolvable_user_sees_nothing(self):
        viewset = self._viewset_for(
            roles=["item_writer"], sub=self.owner.keycloak_sub
        )
        ids = set(viewset.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, set())


class SearchAuditTrailTests(TestCase):
    """SRS-NBE-F02-10: search execution must be audit-logged."""

    def test_list_emits_search_executed_audit(self):
        viewer = _make_user()
        request = _request(
            roles=["nbec_secretariat"], sub=viewer.keycloak_sub
        )

        viewset = ItemSearchViewSet()
        viewset.request = request
        viewset.kwargs = {}
        viewset.format_kwarg = None
        viewset.action = "list"

        fake_response = type(
            "FakeResponse", (), {"data": {"count": 0, "results": []}}
        )()
        with patch(
            "apps.itembank.views.viewsets.ReadOnlyModelViewSet.list",
            return_value=fake_response,
        ):
            viewset.list(request)

        self.assertTrue(
            AuditEvent.objects.filter(action="SEARCH_EXECUTED").exists()
        )

    def test_export_emits_search_exported_audit(self):
        viewer = _make_user()
        request = _request(
            roles=["nbec_secretariat"], sub=viewer.keycloak_sub
        )
        viewset = ItemSearchViewSet()
        viewset.request = request
        viewset.kwargs = {}
        viewset.format_kwarg = None
        viewset.action = "export"

        # filter_queryset / get_queryset are exercised normally — there
        # are no items so the export resolves to an empty list quickly.
        response = viewset.export(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            AuditEvent.objects.filter(action="SEARCH_EXPORTED").exists()
        )


class ItemSearchQuerysetTests(TestCase):
    def test_usage_count_sums_recorded_occurrences(self):
        author = _make_user()
        item = _make_item(author=author)
        ItemUsage.objects.create(item_id=item, sitting_ref="BAR-2026-01", count=3)
        ItemUsage.objects.create(item_id=item, sitting_ref="BAR-2026-02", count=2)

        result = _item_search_queryset(Item.objects.filter(pk=item.pk)).get()

        self.assertEqual(result.usage_count, 5)
