from __future__ import annotations

import pytest

from app.rag.clients import _mineru_parse_fields


def test_mineru_pipeline_fields_remain_backward_compatible(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_BACKEND", raising=False)
    monkeypatch.delenv("MINERU_VLLM_API_HOST", raising=False)

    fields = _mineru_parse_fields()

    assert fields["backend"] == "pipeline"
    assert "server_url" not in fields
    assert "image_analysis" not in fields


@pytest.mark.parametrize(
    ("configured_url", "expected_url"),
    [
        ("http://192.0.2.10:80", "http://192.0.2.10:80"),
        ("http://192.0.2.10:80/v1", "http://192.0.2.10:80"),
        ("http://192.0.2.10:80/v1/models", "http://192.0.2.10:80"),
    ],
)
def test_mineru_http_client_fields_normalize_vllm_url(
    monkeypatch, configured_url: str, expected_url: str
) -> None:
    monkeypatch.setenv("MINERU_BACKEND", "vlm-http-client")
    monkeypatch.setenv("MINERU_VLLM_API_HOST", configured_url)
    monkeypatch.setenv("MINERU_IMAGE_ANALYSIS", "TRUE")

    fields = _mineru_parse_fields()

    assert fields["backend"] == "vlm-http-client"
    assert fields["server_url"] == expected_url
    assert fields["image_analysis"] == "true"


def test_mineru_http_client_requires_vllm_url(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_BACKEND", "vlm-http-client")
    monkeypatch.delenv("MINERU_VLLM_API_HOST", raising=False)

    with pytest.raises(ValueError, match="MINERU_VLLM_API_HOST"):
        _mineru_parse_fields()
