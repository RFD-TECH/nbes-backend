"""apps/users/models.py — Local profile + NBES RBAC catalog.

Identity belongs to IAM/Keycloak; NBES only mirrors the minimum.
Permissions belong to NBES: codenames are domain concepts (e.g. ``item:approve``)
that mean nothing outside this service, so the catalog lives here.

Three RBAC tables:

* ``Permission`` — the catalog of codenames NBES enforces. Codenames are
  declared in code (``HasPermission("...")`` in views) and seeded via
  migration; admins do not invent new codenames at runtime because nothing
  would enforce them.
* ``Role`` — local registry of role names NBES recognises. Mirrors the
  NBES-scoped role names IAM has created in the Keycloak realm (one row per
  role-name that NBES knows about). A JWT can carry roles NBES does not
  recognise — those are ignored.
* ``RolePermission`` — the editable matrix. A system_administrator can grant
  or revoke a codename on a role without a redeploy. This is the bit
  REQ-F000-02 calls "configurable".
"""
import uuid
from django.db import models


class UserProfile(models.Model):
    """Thin local profile. Keycloak owns authentication; never store secrets."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keycloak_sub = models.UUIDField(unique=True, db_index=True)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_userprofile"
        verbose_name = "User Profile"

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def __str__(self):
        return f"{self.email} ({self.role})"


class Permission(models.Model):
    """A permission codename NBES enforces. Seeded; not user-created."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    codename = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_permission"
        ordering = ["codename"]

    def __str__(self):
        return self.codename


class Role(models.Model):
    """NBES-scoped role name. Mirrored from IAM's UserSystemAssignment role_type
    so NBES can ignore JWT roles it does not recognise as its own."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_custom = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "users_role"
        ordering = ["name"]

    def __str__(self):
        return self.name


class RolePermission(models.Model):
    """Editable role → permission grant. The matrix REQ-F000-02 says is
    configurable. Edits propagate within 60s via the rbac cache."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="grants")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="grants")
    granted_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "users_rolepermission"
        unique_together = ("role", "permission")
        ordering = ["role__name", "permission__codename"]

    def __str__(self):
        return f"{self.role.name}:{self.permission.codename}"
