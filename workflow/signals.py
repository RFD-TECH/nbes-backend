"""
workflow/signals.py — Post-FSM-Transition Signal Handlers
==========================================================

Django signals fired after FSM transitions complete.
Register these in the relevant AppConfig.ready() methods.

Pattern:
    django-fsm fires django_fsm.signals.post_transition after every
    transition. Connect handlers here to keep side effects decoupled
    from the model's transition methods.

Reference: NBES System Architecture §3 — Workflow State Machines
"""

from django_fsm.signals import post_transition


def on_item_status_change(sender, instance, name, source, target, **kwargs):
    """
    Fired after any Item FSM transition.
    Used for cross-cutting concerns: audit logging, metric updates.
    TODO: Implement as needed.
    """
    pass


def on_registration_status_change(sender, instance, name, source, target, **kwargs):
    """
    Fired after any Registration FSM transition.
    TODO: Implement as needed.
    """
    pass


def on_script_status_change(sender, instance, name, source, target, **kwargs):
    """
    Fired after any Script FSM transition.
    TODO: Implement as needed.
    """
    pass


def on_result_set_status_change(sender, instance, name, source, target, **kwargs):
    """
    Fired after any ResultSet FSM transition.
    TODO: Implement as needed.
    """
    pass


def connect_signals():
    """
    Call this from AppConfig.ready() in each app that needs signal handlers.
    Example in apps/itembank/apps.py:
        from workflow.signals import connect_signals
        connect_signals()
    """
    from apps.itembank.models import Item
    from apps.registration.models import Registration
    from apps.marking.models import Script
    from apps.results.models import ResultSet

    post_transition.connect(on_item_status_change, sender=Item)
    post_transition.connect(on_registration_status_change, sender=Registration)
    post_transition.connect(on_script_status_change, sender=Script)
    post_transition.connect(on_result_set_status_change, sender=ResultSet)
