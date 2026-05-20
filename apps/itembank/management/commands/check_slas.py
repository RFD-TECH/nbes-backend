"""Management command for checking item review SLAs."""

from django.core.management.base import BaseCommand

from apps.itembank.services import check_and_escalate_overdue_reviews


class Command(BaseCommand):
    """Run the SLA check for items currently in review."""

    help = (
        "Evaluates all items In Review against the 5-day SLA and triggers escalations."
    )

    def handle(self, *args, **options):
        """Execute the SLA check and report the outcome."""
        self.stdout.write("Starting SLA evaluation...")

        # Delegate the escalation decision to the service layer.
        escalated_count = check_and_escalate_overdue_reviews()

        if escalated_count > 0:
            warning_style = getattr(self.style, "WARNING")
            self.stdout.write(
                warning_style(
                    f"SLA Check Complete: {escalated_count} items "
                    "escalated to Moderation Lead."
                )
            )
        else:
            self.stdout.write("SLA Check Complete: 0 items are overdue. All queues healthy.")
