from __future__ import annotations

import asyncio

import main


def test_discord_config_exposes_only_agent_enabled_comfy_generation(monkeypatch) -> None:
    captured = {}
    generated = {}

    async def fake_apply(config, generate, step, research, image):
        captured.update({
            "config": config,
            "generate": generate,
            "step": step,
            "research": research,
            "image": image,
        })

    async def fake_generate_image(**kwargs):
        generated.update(kwargs)
        return {
            "summary": "이미지 생성 완료",
            "image": {
                "baseUrl": "http://127.0.0.1:8188",
                "filename": "result.png",
                "subfolder": "",
                "storageType": "output",
            },
        }

    async def fake_fetch(base_url, filename, subfolder, storage_type):
        assert (base_url, filename, subfolder, storage_type) == (
            "http://127.0.0.1:8188", "result.png", "", "output"
        )
        return b"png", "image/png"

    monkeypatch.setattr(main.discordbot, "apply_config", fake_apply)
    monkeypatch.setattr(main.discordbot, "status", lambda: {"running": False})
    monkeypatch.setattr(main, "generate_comfy_image", fake_generate_image)
    monkeypatch.setattr(main, "fetch_comfy_output_image", fake_fetch)
    config = main.DiscordConfig(
        enabled=False,
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[{"id": "ready", "agentEnabled": True}],
    )

    asyncio.run(main.discord_config_ep(config))
    result = asyncio.run(captured["image"]({
        "prompt": "비 오는 미래 도시",
        "negative_prompt": "blurry",
        "model_hint": "city",
        "width": 1024,
        "height": 1024,
        "seed": "42",
    }))

    assert generated["profiles"] == [{"id": "ready", "agentEnabled": True}]
    assert generated["selected_profile_id"] is None
    assert generated["selection_context"] == "Discord에서 사용자가 요청한 이미지 생성"
    assert result == {
        "summary": "이미지 생성 완료",
        "data": b"png",
        "filename": "result.png",
        "content_type": "image/png",
    }


def test_discord_config_hides_image_tool_without_agent_enabled_profile(monkeypatch) -> None:
    captured = {}

    async def fake_apply(_config, _generate, _step, _research, image):
        captured["image"] = image

    monkeypatch.setattr(main.discordbot, "apply_config", fake_apply)
    monkeypatch.setattr(main.discordbot, "status", lambda: {"running": False})
    config = main.DiscordConfig(
        enabled=False,
        comfy_base_url="http://127.0.0.1:8188",
        comfy_profiles=[{"id": "not-ready", "agentEnabled": False}],
    )

    asyncio.run(main.discord_config_ep(config))

    assert captured["image"] is None
