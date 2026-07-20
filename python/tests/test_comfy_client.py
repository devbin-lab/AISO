# -*- coding: utf-8 -*-
"""ComfyUI 로컬 API 클라이언트와 최소 FastAPI 라우트 테스트.

모든 HTTP 결과는 _get_json을 대체해 구성하므로 실제 네트워크를 사용하지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comfy_client  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:8188", "http://127.0.0.1:8188"),
        ("http://127.0.0.1:8188/", "http://127.0.0.1:8188"),
        ("http://localhost", "http://localhost"),
        ("HTTP://LOCALHOST:8188/", "http://localhost:8188"),
        ("http://[::1]:8188/", "http://[::1]:8188"),
    ],
)
def test_normalize_base_url_allows_exact_loopback(raw, expected):
    assert comfy_client.normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://127.0.0.1:8188",
        "http://192.168.0.10:8188",
        "http://127.0.0.1.example.com:8188",
        "http://2130706433:8188",
        "http://127.1:8188",
        "http://user@127.0.0.1:8188",
        "http://evil.example@127.0.0.1:8188",
        "http://127.0.0.1@evil.example:8188",
        "http://localhost:8188/prefix",
        "http://localhost:8188?next=http://evil.example",
        "http://localhost:8188?",
        "http://localhost:8188#fragment",
        "http://localhost:8188#",
        "http://localhost:",
        "http://localhost:0",
        " http://localhost:8188",
        "http://localhost:8188 ",
        "",
    ],
)
def test_normalize_base_url_rejects_bypass_forms(raw):
    with pytest.raises(comfy_client.InvalidComfyURL):
        comfy_client.normalize_base_url(raw)


def test_health_online_normalizes_versions_and_devices(monkeypatch):
    async def fake_get_json(base_url, route):
        assert base_url == "http://127.0.0.1:8188"
        assert route == "/system_stats"
        return {
            "system": {
                "comfyui_version": "0.28.0",
                "required_frontend_version": "1.45.21",
                "python_version": "ignored",
            },
            "devices": [
                {
                    "name": "cuda:0 NVIDIA RTX",
                    "type": "cuda",
                    "vram_total": 16_000,
                    "vram_free": 12_000,
                    "torch_vram_total": 9_999,
                }
            ],
        }

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_health())
    assert result == {
        "online": True,
        "base_url": "http://127.0.0.1:8188",
        "version": "0.28.0",
        "frontend_version": "1.45.21",
        "devices": [
            {
                "name": "cuda:0 NVIDIA RTX",
                "type": "cuda",
                "vram_total": 16_000,
                "vram_free": 12_000,
            }
        ],
        "detail": None,
    }


def test_health_connection_failure_is_safe_offline_state(monkeypatch):
    request = httpx.Request("GET", "http://127.0.0.1:8188/system_stats")

    async def fake_get_json(_base_url, _route):
        raise httpx.ConnectError("secret upstream detail", request=request)

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_health())
    assert result["online"] is False
    assert result["devices"] == []
    assert result["detail"] == "ComfyUI에 연결할 수 없습니다."
    assert "secret" not in result["detail"]


def test_health_bad_schema_is_safe_offline_state(monkeypatch):
    async def fake_get_json(_base_url, _route):
        return {"system": [], "devices": "not-a-list", "secret": "do not expose"}

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_health())
    assert result["online"] is False
    assert result["detail"] == "ComfyUI 응답 형식이 올바르지 않습니다."
    assert "secret" not in result["detail"]


def test_health_missing_comfy_version_is_offline(monkeypatch):
    async def fake_get_json(_base_url, _route):
        return {"system": {"required_frontend_version": "1.0"}, "devices": []}

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_health())
    assert result["online"] is False
    assert result["version"] is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], []),
        (
            ["zeta.safetensors", "anime/base.safetensors", "zeta.safetensors", "a.ckpt"],
            ["a.ckpt", "anime/base.safetensors", "zeta.safetensors"],
        ),
    ],
)
def test_checkpoints_returns_sorted_unique_strings(monkeypatch, payload, expected):
    async def fake_get_json(base_url, route):
        assert base_url == "http://localhost:8188"
        assert route == "/models/checkpoints"
        return payload

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_checkpoints("http://localhost:8188/"))
    assert result == {"base_url": "http://localhost:8188", "checkpoints": expected}


@pytest.mark.parametrize("payload", [{}, ["ok.safetensors", 7], None])
def test_checkpoints_rejects_bad_schema(monkeypatch, payload):
    async def fake_get_json(_base_url, _route):
        return payload

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(comfy_client.ComfyAPIError) as exc:
        run(comfy_client.get_checkpoints())
    assert "응답 형식" in str(exc.value)
    assert repr(payload) not in str(exc.value)


def test_http_client_uses_three_second_timeout_and_no_redirect_or_env_proxy(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(comfy_client.httpx, "AsyncClient", FakeClient)
    result = run(comfy_client._get_json("http://127.0.0.1:8188", "/system_stats"))
    assert result == {"ok": True}
    assert captured == {
        "timeout": 3.0,
        "follow_redirects": False,
        "trust_env": False,
        "url": "http://127.0.0.1:8188/system_stats",
    }


def test_model_inventory_queries_only_requested_allowed_folders(monkeypatch):
    calls = []

    async def fake_get_json(base_url, route):
        calls.append((base_url, route))
        return {
            "/models/checkpoints": ["z.safetensors", "a.safetensors", "a.safetensors"],
            "/models/vae": ["ae.safetensors"],
        }[route]

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(
        comfy_client.get_models_inventory(
            "http://localhost:8188/", {"vae", "checkpoints"}
        )
    )
    assert result == {
        "checkpoints": ["a.safetensors", "z.safetensors"],
        "vae": ["ae.safetensors"],
    }
    assert calls == [
        ("http://localhost:8188", "/models/checkpoints"),
        ("http://localhost:8188", "/models/vae"),
    ]


@pytest.mark.parametrize("folders", [set(), {"../checkpoints"}, {"checkpoints", "unknown"}])
def test_model_inventory_rejects_empty_or_untrusted_folders(monkeypatch, folders):
    monkeypatch.setattr(
        comfy_client,
        "_get_json",
        lambda *_args, **_kwargs: pytest.fail("network must not be used"),
    )
    with pytest.raises(comfy_client.ComfyAPIError, match="지원하지 않는"):
        run(comfy_client.get_models_inventory("http://127.0.0.1:8188", folders))


def test_node_info_uses_one_encoded_allowlisted_class_name(monkeypatch):
    async def fake_get_json(base_url, route):
        assert base_url == "http://127.0.0.1:8188"
        assert route == "/object_info/CheckpointLoaderSimple"
        return {"CheckpointLoaderSimple": {"python_module": "nodes", "input": {}}}

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    assert run(
        comfy_client.get_node_info("http://127.0.0.1:8188", "CheckpointLoaderSimple")
    )["python_module"] == "nodes"
    with pytest.raises(comfy_client.ComfyAPIError, match="노드 이름"):
        run(comfy_client.get_node_info("http://127.0.0.1:8188", "../SaveImage"))


def test_jobs_capability_probes_local_jobs_schema(monkeypatch):
    async def fake_get_json(base_url, route):
        assert base_url == "http://127.0.0.1:8188"
        assert route == "/api/jobs?limit=1"
        return {
            "jobs": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "status": "completed",
                    "outputs_count": 1,
                }
            ],
            "pagination": {
                "offset": 0,
                "limit": 1,
                "total": 1,
                "has_more": False,
            },
        }

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    assert run(comfy_client.get_jobs_capability("http://127.0.0.1:8188")) == {
        "supported": True,
        "baseUrl": "http://127.0.0.1:8188",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jobs": {}, "pagination": {"total": 0, "has_more": False}},
        {"jobs": [], "pagination": {"total": True, "has_more": False}},
        {"jobs": [], "pagination": {"total": 0, "has_more": 0}},
    ],
)
def test_jobs_capability_rejects_invalid_schema(monkeypatch, payload):
    async def fake_get_json(_base_url, _route):
        return payload

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(comfy_client.ComfyAPIError, match="응답 형식"):
        run(comfy_client.get_jobs_capability("http://127.0.0.1:8188"))


@pytest.mark.parametrize(
    ("error_factory", "expected"),
    [
        (
            lambda request: httpx.ConnectError("connection refused", request=request),
            "연결할 수 없습니다",
        ),
        (
            lambda request: httpx.ReadTimeout("timed out", request=request),
            "응답 시간이 초과",
        ),
        (
            lambda request: httpx.HTTPStatusError(
                "server error",
                request=request,
                response=httpx.Response(500, request=request),
            ),
            "HTTP 500",
        ),
    ],
)
def test_jobs_capability_does_not_mislabel_transport_or_server_error_as_unsupported(
    monkeypatch, error_factory, expected
):
    request = httpx.Request("GET", "http://127.0.0.1:8188/api/jobs?limit=1")

    async def fake_get_json(_base_url, _route):
        raise error_factory(request)

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(comfy_client.ComfyAPIError, match=expected) as exc:
        run(comfy_client.get_jobs_capability("http://127.0.0.1:8188"))
    assert "지원하지 않습니다" not in str(exc.value)


@pytest.mark.parametrize("status_code", [404, 405, 501])
def test_jobs_capability_reports_only_missing_targeted_route_as_unsupported(
    monkeypatch, status_code
):
    calls = []
    request = httpx.Request("GET", "http://127.0.0.1:8188/api/jobs?limit=1")

    async def fake_get_json(_base_url, route):
        calls.append(route)
        response = httpx.Response(status_code, request=request)
        raise httpx.HTTPStatusError("route unavailable", request=request, response=response)

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(comfy_client.ComfyAPIError, match="지원하지 않습니다"):
        run(comfy_client.get_jobs_capability("http://127.0.0.1:8188"))
    assert calls == ["/api/jobs?limit=1"]


def test_submit_prompt_sends_canonical_client_generated_ids(monkeypatch):
    client_id = "11111111-1111-4111-8111-111111111111"
    prompt_id = "22222222-2222-4222-8222-222222222222"
    workflow = {"1": {"class_type": "SaveImage", "inputs": {}}}
    captured = {}

    async def fake_request(base_url, method, route, *, json_body=None):
        captured.update(base_url=base_url, method=method, route=route, json_body=json_body)
        return {"prompt_id": prompt_id, "number": 2.0, "node_errors": {}}

    monkeypatch.setattr(comfy_client, "_request_json", fake_request)
    result = run(
        comfy_client.submit_prompt(
            "http://localhost:8188/",
            workflow,
            client_id=client_id,
            prompt_id=prompt_id,
        )
    )
    assert result == {"promptId": prompt_id, "queueNumber": 2.0, "nodeErrors": {}}
    assert captured == {
        "base_url": "http://localhost:8188",
        "method": "POST",
        "route": "/prompt",
        "json_body": {"prompt": workflow, "client_id": client_id, "prompt_id": prompt_id},
    }


@pytest.mark.parametrize(
    ("workflow", "client_id", "prompt_id"),
    [
        ({}, "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
        ({"x": float("nan")}, "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
        ({"1": {}}, "NOT-A-UUID", "22222222-2222-4222-8222-222222222222"),
        ({"1": {}}, "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-22222222222A"),
    ],
)
def test_submit_prompt_rejects_invalid_workflow_or_ids_before_network(
    monkeypatch, workflow, client_id, prompt_id
):
    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr(comfy_client, "_request_json", should_not_run)
    with pytest.raises(comfy_client.ComfyAPIError):
        run(
            comfy_client.submit_prompt(
                "http://127.0.0.1:8188",
                workflow,
                client_id=client_id,
                prompt_id=prompt_id,
            )
        )


def test_submit_prompt_rejects_nonfinite_queue_number(monkeypatch):
    prompt_id = "22222222-2222-4222-8222-222222222222"

    async def fake_request(*_args, **_kwargs):
        return {"prompt_id": prompt_id, "number": float("inf"), "node_errors": {}}

    monkeypatch.setattr(comfy_client, "_request_json", fake_request)
    with pytest.raises(comfy_client.ComfyAPIError, match="응답 형식"):
        run(
            comfy_client.submit_prompt(
                "http://127.0.0.1:8188",
                {"1": {}},
                client_id="11111111-1111-4111-8111-111111111111",
                prompt_id=prompt_id,
            )
        )


def test_get_job_normalizes_completed_output_references(monkeypatch):
    prompt_id = "22222222-2222-4222-8222-222222222222"

    async def fake_get_json(_base_url, route):
        assert route == f"/api/jobs/{prompt_id}"
        return {
            "id": prompt_id,
            "status": "completed",
            "outputs": {
                "7": {
                    "images": [
                        {
                            "filename": "Aiso_00001_.png",
                            "subfolder": "Aiso/session-1",
                            "type": "output",
                        }
                    ]
                }
            },
        }

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    result = run(comfy_client.get_job("http://127.0.0.1:8188", prompt_id))
    assert result == {
        "promptId": prompt_id,
        "status": "completed",
        "terminal": True,
        "outputs": [
            {
                "nodeId": "7",
                "index": 0,
                "filename": "Aiso_00001_.png",
                "subfolder": "Aiso/session-1",
                "storageType": "output",
            }
        ],
        "error": None,
    }


@pytest.mark.parametrize(
    "image",
    [
        {"filename": "../secret.png", "subfolder": "", "type": "output"},
        {"filename": "image.png", "subfolder": "../secret", "type": "output"},
        {"filename": "image.svg", "subfolder": "", "type": "output"},
        {"filename": "image.png", "subfolder": "", "type": "temp"},
    ],
)
def test_get_job_rejects_untrusted_output_references(monkeypatch, image):
    prompt_id = "22222222-2222-4222-8222-222222222222"

    async def fake_get_json(_base_url, _route):
        return {
            "id": prompt_id,
            "status": "completed",
            "outputs": {"7": {"images": [image]}},
        }

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(comfy_client.ComfyAPIError, match="이미지 설명자"):
        run(comfy_client.get_job("http://127.0.0.1:8188", prompt_id))


def test_cancel_job_calls_only_targeted_jobs_endpoint(monkeypatch):
    prompt_id = "22222222-2222-4222-8222-222222222222"
    calls = []

    async def fake_request(base_url, method, route, *, json_body=None):
        calls.append((base_url, method, route, json_body))
        return {"cancelled": True}

    monkeypatch.setattr(comfy_client, "_request_json", fake_request)
    assert run(comfy_client.cancel_job("http://localhost:8188/", prompt_id)) == {
        "promptId": prompt_id,
        "cancelled": True,
    }
    assert calls == [
        ("http://localhost:8188", "POST", f"/api/jobs/{prompt_id}/cancel", None)
    ]
    assert all("interrupt" not in route for _, _, route, _ in calls)


def test_release_models_posts_empty_response_contract_without_proxy_or_redirect(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            captured.update(url=url, json=json)
            return FakeResponse()

    monkeypatch.setattr(comfy_client.httpx, "AsyncClient", FakeClient)
    assert run(comfy_client.release_models("http://localhost:8188/")) == {"requested": True}
    assert captured == {
        "timeout": 3.0,
        "follow_redirects": False,
        "trust_env": False,
        "url": "http://localhost:8188/free",
        "json": {"unload_models": True, "free_memory": True},
    }


class _FakeStreamResponse:
    def __init__(self, *, headers=None, chunks=()):
        self.headers = headers or {}
        self._chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


def _install_stream_client(monkeypatch, response):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, params):
            captured.update(method=method, url=url, params=params)
            return _FakeStreamContext(response)

    monkeypatch.setattr(comfy_client.httpx, "AsyncClient", FakeClient)
    return captured


def test_fetch_output_image_streams_validated_reference_without_proxy_or_redirect(monkeypatch):
    response = _FakeStreamResponse(
        headers={"content-type": "image/png; charset=binary", "content-length": "6"},
        chunks=(b"abc", b"def"),
    )
    captured = _install_stream_client(monkeypatch, response)
    content, mime = run(
        comfy_client.fetch_output_image(
            "http://localhost:8188/", "result.png", "Aiso/session", "output"
        )
    )
    assert (content, mime) == (b"abcdef", "image/png")
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["timeout"].connect == 3.0
    assert captured["method"] == "GET"
    assert captured["url"] == "http://localhost:8188/view"
    assert captured["params"] == {
        "filename": "result.png",
        "subfolder": "Aiso/session",
        "type": "output",
    }


@pytest.mark.parametrize(
    ("filename", "subfolder", "storage_type"),
    [
        ("../secret.png", "", "output"),
        ("C:\\secret.png", "", "output"),
        ("result.png\x00", "", "output"),
        ("result.svg", "", "output"),
        ("result.png", "../secret", "output"),
        ("result.png", "", "input"),
    ],
)
def test_fetch_output_image_rejects_untrusted_reference_before_network(
    monkeypatch, filename, subfolder, storage_type
):
    class ShouldNotConstruct:
        def __init__(self, **_kwargs):
            raise AssertionError("network must not be used")

    monkeypatch.setattr(comfy_client.httpx, "AsyncClient", ShouldNotConstruct)
    with pytest.raises(comfy_client.ComfyAPIError):
        run(
            comfy_client.fetch_output_image(
                "http://127.0.0.1:8188", filename, subfolder, storage_type
            )
        )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"content-type": "text/html"}, "이미지 형식"),
        ({"content-type": "image/jpeg"}, "이미지 형식"),
        ({"content-type": "image/png", "content-length": "invalid"}, "크기 응답"),
        (
            {"content-type": "image/png", "content-length": str(50 * 1024 * 1024 + 1)},
            "50 MiB",
        ),
    ],
)
def test_fetch_output_image_rejects_mime_or_declared_size(monkeypatch, headers, expected):
    _install_stream_client(monkeypatch, _FakeStreamResponse(headers=headers, chunks=()))
    with pytest.raises(comfy_client.ComfyAPIError, match=expected):
        run(comfy_client.fetch_output_image("http://127.0.0.1:8188", "result.png"))


def test_fetch_output_image_stops_when_stream_crosses_size_limit(monkeypatch):
    monkeypatch.setattr(comfy_client, "_MAX_IMAGE_BYTES", 5)
    response = _FakeStreamResponse(
        headers={"content-type": "image/png"},
        chunks=(b"123", b"456"),
    )
    _install_stream_client(monkeypatch, response)
    with pytest.raises(comfy_client.ComfyAPIError, match="50 MiB"):
        run(comfy_client.fetch_output_image("http://127.0.0.1:8188", "result.png"))
