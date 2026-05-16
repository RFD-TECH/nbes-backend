"""
workflow/viewflow/ratification.py — Board Ratification Flow
============================================================

Multi-party Board ratification using django-viewflow.
Quorum: simple majority of active NBEC voting members (configurable, default 5).
Each member casts one vote: Approve / Reject.
Chair signs the ratification record on quorum reached (HSM-backed in production).
Record is immutable from that point — amendments require addendum.

TODO: Implement using viewflow library once installed and configured.
      See NBES System Architecture §3.5.1 — Board Ratification viewflow process.

Reference:
    viewflow documentation: https://docs.viewflow.io/
    Architecture doc §3.5.1 — BoardRatificationFlow
"""


class BoardRatificationFlow:
    """
    Stub — to be implemented with django-viewflow.

    Flow steps:
        1. start_ratification(result_set)    — initiates the flow
        2. notify_members()                  — alerts all NBEC voting members
        3. cast_vote() [parallel, per member]— each member votes Approve/Reject
        4. check_quorum()                    — evaluates if majority reached
        5. chair_sign()                      — Chair signs ratification record
        6. finalise()                        — calls ResultSet.complete_ratification() FSM transition

    Permissions:
        cast_vote:  results.can_ratify  → nbec-member role
        chair_sign: results.can_chair_sign → nbec-member + Chair designation

    TODO: Replace this stub with full viewflow.workflow.flow.Flow subclass.
    """

    @classmethod
    def start(cls, result_set):
        """
        Initiate the Board ratification flow for a ResultSet.
        Called by ResultSet.open_board_review() FSM transition method.

        TODO: Implement viewflow process start.
        """
        raise NotImplementedError(
            "BoardRatificationFlow not yet implemented. "
            "See workflow/viewflow/ratification.py and NBES architecture §3.5.1."
        )
