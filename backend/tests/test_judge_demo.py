import pytest

from app.judge_demo import validate_environment


def settings(monkeypatch):
    for key, value in {
        "DATABASE_URL": "postgresql+psycopg://mica_judge:test@db.example/mica_judge_demo",
        "PROVIDER": "nebius",
        "NEBIUS_MODEL": "nvidia/Nemotron-3.5-Super-VL",
        "TELEGRAM_BOT_TOKEN": "",
        "CHANNEL_BRIDGE_URL": "",
        "CHANNEL_BRIDGE_TOKEN": "",
        "CHANNEL_ACCOUNT_ID": "",
    }.items():
        monkeypatch.setenv(key, value)


def test_isolated_demo_config_is_accepted(monkeypatch):
    settings(monkeypatch)
    validate_environment()


@pytest.mark.parametrize(
    "key,value",
    [
        ("DATABASE_URL", "postgresql+psycopg://owner:test@db.example/mica_judge_demo"),
        ("DATABASE_URL", "postgresql+psycopg://mica_judge:test@db.example/private_owner"),
        ("PROVIDER", "azure_openai"),
        ("TELEGRAM_BOT_TOKEN", "private-bot"),
        ("CHANNEL_BRIDGE_URL", "http://bridge"),
    ],
)
def test_private_identity_or_external_channel_fails_closed(monkeypatch, key, value):
    settings(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        validate_environment()
