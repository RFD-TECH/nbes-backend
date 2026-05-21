"""Service-layer tests for Sprint 3.4 paper construction and quality flow."""
from decimal import Decimal
from uuid import uuid4

from django.test import TestCase, override_settings

from apps.audit.models import AuditEvent
from apps.itembank.models import (
    Item,
    ItemUsage,
    Paper,
    VaultAccess,
)
from apps.itembank.services import (
    _recent_sittings,
    create_manual_paper,
    generate_paper_rule_based,
    submit_paper_for_approval,
)
from django.contrib.auth import get_user_model


def _make_user(sub=None):
    """Create the auth User Item.author_id expects + attach keycloak_sub.

    Services read ``user.keycloak_sub`` to populate audit and vault rows.
    The local auth model doesn't have that column, so we attach it as a
    runtime attribute after creation.
    """
    User = get_user_model()
    username = f"u-{uuid4().hex[:10]}"
    user = User.objects.create(
        username=username, email=f"{username}@example.test"
    )
    user.keycloak_sub = sub or uuid4()
    return user


def _make_item(
    *,
    author,
    subject="Contract Law",
    topic="Contract Law",
    difficulty="Easy",
    marks=Decimal("5.00"),
    time=300,
    status=Item.Status.LOCKED_FOR_USE,
):
    return Item.objects.create(
        author_id=author,
        subject=subject,
        topic=topic,
        difficulty=difficulty,
        cognitive_level="Knowledge",
        marks=marks,
        time=time,
        status=status,
    )


class RecentSittingsTests(TestCase):
    """Regression test for the PostgreSQL-incompatible cool-down query."""

    def test_recent_sittings_orders_by_latest_recorded(self):
        author = _make_user()
        item = _make_item(author=author)
        ItemUsage.objects.create(item_id=item, sitting_ref="BAR-2025-12")
        ItemUsage.objects.create(item_id=item, sitting_ref="BAR-2026-03")
        ItemUsage.objects.create(item_id=item, sitting_ref="BAR-2026-01")

        recent = _recent_sittings(2)

        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0], "BAR-2026-01")
        self.assertIn("BAR-2026-03", recent)


@override_settings(
    ITEM_COOLDOWN_SITTINGS=3, PAPER_MARKS_TOLERANCE="0", NBES_BLUEPRINTS={}
)
class CreateManualPaperTests(TestCase):
    def setUp(self):
        self.author = _make_user()
        self.actor = _make_user()
        self.items = [
            _make_item(author=self.author, marks=Decimal("10.00"))
            for _ in range(2)
        ]

    def _payload(self, **overrides):
        base = {
            "item_ids": [item.id for item in self.items],
            "sitting_ref": "BAR-2026-06",
            "subject": "Contract Law",
            "mode": "CBT",
            "total_marks": Decimal("20.00"),
            "time_limit": 3600,
        }
        base.update(overrides)
        return base

    def test_happy_path_creates_paper_and_vault_reads(self):
        paper = create_manual_paper(self._payload(), self.actor)

        self.assertEqual(paper.status, Paper.Status.CONSTRUCTED)
        self.assertEqual(len(paper.item_ids), 2)
        self.assertEqual(
            VaultAccess.objects.filter(actor_id=self.actor, kind="read").count(), 2
        )
        self.assertTrue(
            AuditEvent.objects.filter(action="PAPER_CONSTRUCTED").exists()
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="VAULT_READ").count(), 2
        )

    def test_rejects_duplicate_item_ids(self):
        payload = self._payload(item_ids=[self.items[0].id, self.items[0].id])
        with self.assertRaisesMessage(ValueError, "Duplicate item_ids"):
            create_manual_paper(payload, self.actor)

    def test_rejects_non_locked_items(self):
        self.items[0].status = Item.Status.DRAFT
        self.items[0].save(update_fields=["status"])
        with self.assertRaisesMessage(ValueError, "Locked for Use"):
            create_manual_paper(self._payload(), self.actor)

    def test_marks_mismatch_reports_srs_wording(self):
        payload = self._payload(total_marks=Decimal("999.00"))
        with self.assertRaisesRegex(
            ValueError, "do not match the configured paper total"
        ):
            create_manual_paper(payload, self.actor)

    def test_time_allocation_overflow_rejected(self):
        with self.assertRaisesRegex(ValueError, "Time allocation exceeds"):
            create_manual_paper(self._payload(time_limit=100), self.actor)

    def test_blueprint_violation_under_representation(self):
        with override_settings(
            NBES_BLUEPRINTS={
                "BP-1": {
                    "topics": {"Contract Law": 50, "Tort Law": 50},
                }
            }
        ):
            payload = self._payload(blueprint_ref="BP-1")
            with self.assertRaisesRegex(
                ValueError, "Topic coverage does not satisfy the blueprint"
            ):
                create_manual_paper(payload, self.actor)

    def test_sections_must_cover_all_items(self):
        payload = self._payload(
            sections=[{"name": "Section A", "item_ids": [self.items[0].id]}]
        )
        with self.assertRaisesRegex(
            ValueError, "Section structure does not cover every item"
        ):
            create_manual_paper(payload, self.actor)

    def test_cool_down_blocks_recently_used_items(self):
        ItemUsage.objects.create(item_id=self.items[0], sitting_ref="BAR-2026-05")
        with self.assertRaisesRegex(ValueError, "cool-down window"):
            create_manual_paper(self._payload(), self.actor)

    def test_submit_for_approval_transitions_state(self):
        paper = create_manual_paper(self._payload(), self.actor)
        paper = submit_paper_for_approval(paper.id, self.actor)
        self.assertEqual(paper.status, Paper.Status.READY_FOR_APPROVAL)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="PAPER_SUBMITTED_FOR_APPROVAL"
            ).exists()
        )


@override_settings(ITEM_COOLDOWN_SITTINGS=3, PAPER_MARKS_TOLERANCE="5.0")
class GeneratePaperRuleBasedTests(TestCase):
    def setUp(self):
        self.author = _make_user()
        self.actor = _make_user()
        # 8 items per (diff, topic) bucket of 5 marks each so we have enough
        # supply to build two disjoint variants under a clean 50/50 split.
        for diff in ("Easy", "Hard"):
            for topic in ("Contract Law", "Tort Law"):
                for _ in range(8):
                    _make_item(
                        author=self.author,
                        difficulty=diff,
                        topic=topic,
                        subject="Contract Law",
                        marks=Decimal("5.00"),
                        time=60,
                    )

    def _payload(self, **overrides):
        base = {
            "sitting_ref": "BAR-2026-06",
            "subject": "Contract Law",
            "mode": "CBT",
            # Bucket math: 60 total × 50% diff × 50% topic = 15 marks per
            # bucket → 3× 5-mark items per bucket exactly, 12 items total.
            "total_marks": Decimal("60.00"),
            "time_limit": 5400,
            "difficulty_distribution": {"Easy": 50, "Hard": 50},
            "topic_coverage": {"Contract Law": 50, "Tort Law": 50},
        }
        base.update(overrides)
        return base

    def test_distribution_must_sum_to_100(self):
        with self.assertRaisesRegex(ValueError, "Difficulty distribution"):
            generate_paper_rule_based(
                self._payload(difficulty_distribution={"Easy": 10}), self.actor
            )

    def test_generates_paper_and_logs_vault_reads(self):
        paper = generate_paper_rule_based(self._payload(), self.actor)
        self.assertEqual(paper.status, Paper.Status.CONSTRUCTED)
        self.assertTrue(len(paper.item_ids) > 0)
        self.assertEqual(
            VaultAccess.objects.filter(actor_id=self.actor, kind="read").count(),
            len(paper.item_ids),
        )

    def test_variants_are_disjoint(self):
        paper = generate_paper_rule_based(
            self._payload(variants_count=2), self.actor
        )
        primary = set(paper.item_ids)
        self.assertEqual(len(paper.variants), 1)
        variant_items = set(paper.variants[0]["item_ids"])
        self.assertTrue(primary.isdisjoint(variant_items))
