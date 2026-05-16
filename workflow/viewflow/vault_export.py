"""
workflow/viewflow/vault_export.py — Vault Multi-Party Export Flow
=================================================================

2-of-3 NBEC officer co-authorisation for vault export.
Any 2 of 3 designated NBEC officers must authorise before items are decrypted
and packaged for System 10B (CBT delivery).

TODO: Implement using django-viewflow.
      See NBES System Architecture §4.2 — Vault Export Flow.

Reference:
    viewflow documentation: https://docs.viewflow.io/
    Architecture doc §4 — Vault & Multi-Party Authorisation
"""


class VaultExportFlow:
    """
    Stub — to be implemented with django-viewflow.

    Flow steps:
        1. start(export_request)         — Officer 1 initiates export
        2. notify_officers()             — Real-time alert to Chair and DG
        3. co_sign() [requires 2 distinct officers] — Officer 2 co-signs
        4. check_authorisation()         — Validates 2 distinct authorisers
        5. execute_export()              — Decrypts items, packages, pushes to System 10B
        6. record_audit()                — Full audit: initiator, co-signer, items, timestamp, IP

    Permissions:
        start:    item:vault:export → nbec-member role
        co_sign:  item:vault:export → nbec-member role (different officer from initiator)

    TODO: Replace this stub with full viewflow.workflow.flow.Flow subclass.
    """

    @classmethod
    def start(cls, export_request, initiated_by):
        """
        Initiate a vault export authorisation flow.

        TODO: Implement viewflow process start.
        """
        raise NotImplementedError(
            "VaultExportFlow not yet implemented. "
            "See workflow/viewflow/vault_export.py and NBES architecture §4.2."
        )
