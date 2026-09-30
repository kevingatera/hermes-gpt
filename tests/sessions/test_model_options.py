"""Per-turn choices remain explicit and never resolve provider credentials."""

from dataclasses import FrozenInstanceError

import pytest

from hermes_gpt.sessions.model_options import REASONING_EFFORTS, ModelOverrides


def test_defaults_remain_unresolved():
    overrides = ModelOverrides()
    assert overrides.model is None
    assert overrides.reasoning_effort is None


@pytest.mark.parametrize("effort", sorted(REASONING_EFFORTS))
def test_each_supported_effort_is_an_independent_override(effort):
    overrides = ModelOverrides(reasoning_effort=effort)
    assert overrides.model is None
    assert overrides.reasoning_effort == effort


def test_custom_provider_and_model_can_be_selected_without_effort():
    overrides = ModelOverrides("local-provider/model-v1:fast")
    assert overrides.model == "local-provider/model-v1:fast"
    assert overrides.reasoning_effort is None


@pytest.mark.parametrize("model", ["", "bare-model", "provider/", "/model", "a/ b", "a/\n", 1, True])
def test_invalid_model_is_rejected(model):
    with pytest.raises(ValueError, match="model must"):
        ModelOverrides(model=model)


def test_model_length_boundary():
    model = "a/" + "b" * 254
    assert ModelOverrides(model=model).model == model
    with pytest.raises(ValueError, match="valid provider/model"):
        ModelOverrides(model=model + "b")


@pytest.mark.parametrize("effort", ["", "HIGH", "automatic", 1, True])
def test_invalid_effort_is_rejected(effort):
    with pytest.raises(ValueError, match="reasoning_effort must"):
        ModelOverrides(reasoning_effort=effort)


def test_validated_choices_cannot_be_replaced_after_construction():
    overrides = ModelOverrides("provider/model", "low")
    with pytest.raises(FrozenInstanceError):
        overrides.model = "invalid"
