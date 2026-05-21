"""apps/sitting/permissions.py — RBAC codenames used by Phase 4 endpoints.

These codenames are already seeded into the RBAC matrix
(see ``apps/users/migrations/0001_initial.py``) so the
``shared.permissions.has_permission`` decorator can resolve them at runtime
without further setup.

* ``sitting:configure`` — held by ``nbec_member``. Required for create / draft
  edit / configure / approve / amendment / variant generation.
* ``sitting:lock:override`` — held by ``nbec_member``. Required to manually
  lock or override the T-30 lock window.
"""

PERM_SITTING_CONFIGURE = "sitting:configure"
PERM_SITTING_LOCK_OVERRIDE = "sitting:lock:override"
