from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.gateway import GatewayError, OpenAICompatibleGateway

AZURE = {
    "PROVIDER": "azure_openai",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "test-azure-key",
    "AZURE_OPENAI_DEPLOYMENT": "pet-deployment",
    "AZURE_OPENAI_API_VERSION": "2024-10-21",
    "MODEL_REASONING_EFFORT": "none",
}


def test_azure_url_headers_tokens_and_tools(monkeypatch):
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = json.dumps({
        "model": "gpt-5.4", "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 3},
    }).encode()
    monkeypatch.setattr("app.gateway.build_opener", lambda *_: opener)
    gateway = OpenAICompatibleGateway.from_env(AZURE)
    tools = [{"type": "function", "function": {"name": "save_idea", "parameters": {
        "type": "object", "properties": {},
    }}}]
    result = gateway._complete([{"role": "user", "content": "Hello"}], tools)
    request = opener.open.call_args.args[0]
    assert request.full_url == (
        "https://example.openai.azure.com/openai/deployments/pet-deployment/"
        "chat/completions?api-version=2024-10-21"
    )
    assert request.get_header("Api-key") == "test-azure-key"
    assert request.get_header("Authorization") is None
    body = json.loads(request.data)
    assert body["model"] == "pet-deployment"
    assert body["max_completion_tokens"] == 600
    assert "max_tokens" not in body
    assert body["reasoning_effort"] == "none"
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"
    assert result.input_tokens == 15
    assert result.cost_usd is None
    assert "test-azure-key" not in repr(gateway)
    gateway._complete([{"role": "user", "content": "Hello"}], [])
    without_tools = json.loads(opener.open.call_args.args[0].data)
    assert "tools" not in without_tools
    assert "tool_choice" not in without_tools


@pytest.mark.parametrize("key", [
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
])
def test_missing_azure_settings_fail_closed(key):
    with pytest.raises(GatewayError, match="^invalid_config$"):
        OpenAICompatibleGateway.from_env({k: v for k, v in AZURE.items() if k != key})


@pytest.mark.parametrize("key,value", [
    ("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/openai/deployments/other"),
    ("AZURE_OPENAI_DEPLOYMENT", "../other"),
    ("AZURE_OPENAI_API_VERSION", "2024-10-21&key=secret"),
    ("MODEL_REASONING_EFFORT", "unbounded"),
])
def test_invalid_azure_configuration(key, value):
    with pytest.raises(GatewayError, match="^invalid_config$"):
        OpenAICompatibleGateway.from_env(AZURE | {key: value})


def test_reference_file_imports_only_azure_and_allows_explicit_overrides(tmp_path):
    source = tmp_path / ".env"
    source.write_text("\n".join(f"{k}={v}" for k, v in AZURE.items()) +
                      "\nMODEL_TIMEOUT_SECONDS=999\nDATABASE_URL=not-imported\n")
    gateway = OpenAICompatibleGateway.from_env({
        "PROVIDER": "azure_openai", "AZURE_OPENAI_CONFIG_FILE": str(source),
        "AZURE_OPENAI_DEPLOYMENT": "explicit-deployment",
    })
    assert gateway.model == "explicit-deployment"
    assert gateway.api_key == "test-azure-key"
    assert gateway.timeout_seconds == 15
    assert gateway.reasoning_effort is None


def test_missing_reference_file_is_safe(tmp_path):
    with pytest.raises(GatewayError, match="^invalid_config$"):
        OpenAICompatibleGateway.from_env({
            "PROVIDER": "azure_openai", "AZURE_OPENAI_CONFIG_FILE": str(tmp_path / "missing"),
        })
