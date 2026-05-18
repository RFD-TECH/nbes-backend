"""manage.py bootstrap_admin — Create the first system administrator.

This is the only way to seed an admin account on a fresh deployment.
After it runs, all subsequent admin creation goes through the API and is
subject to the two-Administrator approval rule for high-privilege roles.

Refuses to run if any system-administrator already exists — a deployed
system never needs this command twice.

Usage:
    python manage.py bootstrap_admin \\
        --email admin@gsl.edu.gh \\
        --first-name Ada \\
        --last-name Mensah \\
        --password "SuperSecret!2026"

If --password is omitted, a strong random password is generated and printed
once. The administrator must change it on first login.
"""
import secrets
import string

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.audit.models import AuditEvent
from apps.users.models import PasswordHistory, UserProfile


ADMIN_ROLE = "system-administrator"


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(20))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*-_=+" for c in candidate)
        ):
            return candidate


class Command(BaseCommand):
    help = "Create the first system-administrator account. Refuses to run if one already exists."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Administrator email address.")
        parser.add_argument("--first-name", default="System", help="First name.")
        parser.add_argument("--last-name", default="Administrator", help="Last name.")
        parser.add_argument("--password", default=None, help="Password. Generated if omitted.")

    @transaction.atomic
    def handle(self, *args, **opts):
        if UserProfile.objects.filter(role=ADMIN_ROLE).exclude(status=UserProfile.Status.DEACTIVATED).exists():
            raise CommandError(
                "A system-administrator already exists. "
                "Create additional admins through the API."
            )

        email = opts["email"].strip().lower()
        if UserProfile.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email {email} already exists.")

        password = opts["password"] or _generate_password()
        generated = opts["password"] is None

        user = UserProfile.objects.create(
            email=email,
            first_name=opts["first_name"],
            last_name=opts["last_name"],
            role=ADMIN_ROLE,
            status=UserProfile.Status.ACTIVE,
        )
        user.set_password(password)
        user.save(update_fields=["password_hash", "password_changed_at", "updated_at"])
        PasswordHistory.objects.create(user=user, password_hash=user.password_hash)

        AuditEvent.record(
            actor_id=None,  # System action — no human actor.
            action="BOOTSTRAP_ADMIN_CREATED",
            entity_type="user",
            entity_id=user.id,
            new_state={"email": email, "role": ADMIN_ROLE},
        )

        self.stdout.write(self.style.SUCCESS(f"Created system administrator: {email}"))
        if generated:
            self.stdout.write(self.style.WARNING(
                "Generated password (shown ONCE — record it now):"
            ))
            self.stdout.write(self.style.WARNING(f"    {password}"))
            self.stdout.write(
                "The administrator should change this password on first login."
            )
