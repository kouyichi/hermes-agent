"""Numeric constrains in the dashboard config schema (#147540).

Ratio-valued settings used to reach the UI as a bare ``{"type": "number"}``, so the
native spinner (and ArrowUp/ArrowDown) stepped them by the HTML default of 1 —
Compression Threshold 0.5 became 1.5 or -0.5 in one click, and a 0.05 nudge was
impossible. The schema now carries ``min``/``max``/``step`` for those fields, and
these tests pin both halves: the constraints exist where the unit is a ratio, and
nothing else changes for plain counts.
"""

import pytest

from hermes_cli.web_server_config import CONFIG_SCHEMA, _schema_with_dynamic_provider_options

RATIO_FIELDS = ("compression.threshold", "compression.target_ratio")


@pytest.mark.parametrize("key", RATIO_FIELDS)
def test_ratio_fields_carry_a_sub_unit_step(key):
    entry = CONFIG_SCHEMA[key]
    assert entry["type"] == "number"
    assert entry["step"] == 0.05
    assert entry["min"] == 0.0
    assert entry["max"] == 1.0


@pytest.mark.parametrize("key", RATIO_FIELDS)
def test_ratio_constraints_survive_the_per_request_overlay(key):
    """``/api/config/schema`` serves ``_schema_with_dynamic_provider_options()``.

    That overlay rebuilds entries for the discoverable ``*.provider`` selects; a field
    it does not touch has to come through unchanged, constraints included.
    """
    assert _schema_with_dynamic_provider_options()[key] == CONFIG_SCHEMA[key]


def test_count_fields_keep_the_default_step():
    """The fix is scoped: a count keeps stepping by 1 rather than gaining a ratio step."""
    entry = CONFIG_SCHEMA["compression.protect_last_n"]
    assert entry["type"] == "number"
    assert "step" not in entry
    assert "min" not in entry


def test_ratio_descriptions_are_kept():
    """A partial override must still leave the inferred description in place."""
    for key in RATIO_FIELDS:
        assert CONFIG_SCHEMA[key]["description"]
        assert CONFIG_SCHEMA[key]["category"] == "compression"
