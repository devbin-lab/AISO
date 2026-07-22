# -*- coding: utf-8 -*-
"""FastAPI agent request contract for ComfyUI model selection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import AgentRequest  # noqa: E402


def _payload(**overrides):
    payload = {
        "messages": [{"role": "user", "content": "이미지를 생성해줘"}],
        "workspace": "",
    }
    payload.update(overrides)
    return payload


def test_agent_request_accepts_an_exact_manual_profile_id():
    request = AgentRequest.model_validate(
        _payload(
            comfy_selection_mode="manual",
            selected_comfy_model_id="profile_flux2-klein.4b",
        )
    )
    assert request.comfy_selection_mode == "manual"
    assert request.selected_comfy_model_id == "profile_flux2-klein.4b"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(comfy_selection_mode="recommended"),
        _payload(comfy_selection_mode="manual", selected_comfy_model_id="../outside"),
        _payload(comfy_selection_mode="manual", selected_comfy_model_id="x" * 129),
    ],
)
def test_agent_request_rejects_non_contract_selection_values(payload):
    with pytest.raises(ValidationError):
        AgentRequest.model_validate(payload)
