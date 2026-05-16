"""apps/users/models.py — Thin UserProfile keyed on Keycloak sub UUID."""
import uuid
from django.db import models


class UserProfile(models.Model):
    """
    Thin local profile. Keycloak owns all authentication.
    Created on first authenticated request via get_or_create in shared/auth.py.
    Do NOT store passwords or auth tokens here.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True, db_index=True)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_userprofile"
        verbose_name = "User Profile"

    def __str__(self):
        return f"{self.email} ({self.role})"
