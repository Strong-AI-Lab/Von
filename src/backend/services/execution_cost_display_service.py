"""Represented display preferences; no billing or spending authority."""

import json
import logging
import re
from decimal import Decimal

from .text_value_service import get_texts_for_concept

PREDICATE = "#V#hasExecutionCostDisplayPreferences"
DEFAULTS = {"currency": "USD", "display_above": "0.01", "alert_above": "0.10"}
logger = logging.getLogger(__name__)


def validate_preferences(value):
    """An atomic pair of non-negative USD decimal strings, or {} to inherit."""
    if value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError("execution_cost_preferences_require_currency_and_both_thresholds")
    if value["currency"] != "USD":
        raise ValueError("execution_cost_preferences_require_USD")
    for key in ("display_above", "alert_above"):
        amount = value[key]
        if not isinstance(amount, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,18})?", amount):
            raise ValueError("execution_cost_threshold_requires_non_negative_decimal_string")
    if Decimal(value["alert_above"]) < Decimal(value["display_above"]):
        raise ValueError("execution_cost_alert_must_not_precede_display")
    return dict(value)


def resolve_preferences(user_concept_id, organisation_concept_id=None):
    """Trusted actor/current organisation only; invalid records inherit as a pair."""
    for scope, subject in (("user", user_concept_id), ("organisation", organisation_concept_id)):
        if not subject:
            continue
        try:
            rows = get_texts_for_concept(subject, predicate=PREDICATE, limit=2)
            if len(rows) != 1:
                continue
            value = validate_preferences(json.loads(rows[0]["text"]))
            if value:
                return {**value, "source": scope}
        except (ValueError, TypeError, KeyError):
            logger.warning("Invalid execution cost display preference; inheriting")
        except Exception:
            logger.warning("Execution cost display preference unavailable; inheriting")
    return {**DEFAULTS, "source": "default"}
