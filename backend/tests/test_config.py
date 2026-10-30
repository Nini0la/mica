import os
import subprocess
import sys


def test_production_rejects_deterministic_model():
    env = os.environ | {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://unused:unused@localhost/unused",
        "ACCESS_CODE": "long-production-access-code",
        "SESSION_SECRET": "s" * 32,
        "ALLOWED_ORIGINS": "https://continuity.example",
        "MODEL_MODE": "deterministic",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Deterministic model disabled in production" in result.stderr
