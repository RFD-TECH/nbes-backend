"""
shared/validators.py — Shared Field Validators
"""

import re
from django.core.exceptions import ValidationError


def validate_index_number(value: str) -> None:
    """Format: BAR-YYYY-CCCCC e.g. BAR-2026-00042"""
    if not re.match(r"^BAR-\d{4}-\d{5}$", value):
        raise ValidationError(
            f"'{value}' is not a valid index number. Expected format: BAR-YYYY-CCCCC."
        )


def validate_sitting_ref(value: str) -> None:
    """Format: BAR-YYYY-MM e.g. BAR-2026-05"""
    if not re.match(r"^BAR-\d{4}-\d{2}$", value):
        raise ValidationError(
            f"'{value}' is not a valid sitting reference. Expected format: BAR-YYYY-MM."
        )


def validate_ghana_phone(value: str) -> None:
    """Accepts +233XXXXXXXXX or 0XXXXXXXXX"""
    if not re.match(r"^(\+233|0)\d{9}$", value):
        raise ValidationError(
            f"'{value}' is not a valid Ghana phone number."
        )
