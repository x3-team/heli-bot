"""A .env restored from the repo templates must not open the shared account or carry secrets."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "infra" / "deploy"
SECRET_KEYS = (
    "JWT_SECRET",
    "TRAJECTORY_HASH_PEPPER",
    "HELI_API_KEY",
    "VAULT_MASTER_KEY",
    "TELEGRAM_BOT_TOKEN",
    "YANDEX_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "HELI_CDEK_EDU_CLIENT_SECRET",
)


def _env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


@pytest.mark.skipif(not DEPLOY.is_dir(), reason="infra/ is not shipped with this copy")
def test_env_template_is_closed_and_has_no_secrets() -> None:
    values = _env((DEPLOY / "env.venv.example").read_text(encoding="utf-8"))
    assert values["HELI_OPEN_ACCESS"] == "0"
    assert values["HELI_INVITE_ONLY"] == "1"
    for key in SECRET_KEYS:
        assert values.get(key, "") == "", key


@pytest.mark.skipif(shutil.which("bash") is None or not DEPLOY.is_dir(), reason="needs bash and infra/")
def test_ensure_open_access_closes_a_fresh_env(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HELI_OPEN_ACCESS=1\nHELI_OPEN_ACCESS_EMAIL=owner@heli.local\n", encoding="utf-8")
    environ = {k: v for k, v in os.environ.items() if k != "HELI_DEPLOY_ADMIN_EMAILS"}
    subprocess.run(
        ["bash", str(DEPLOY / "ensure-open-access-in-env.sh"), str(env_file)],
        check=True,
        capture_output=True,
        env=environ,
    )
    values = _env(env_file.read_text(encoding="utf-8"))
    assert values["HELI_OPEN_ACCESS"] == "0"
    assert values["HELI_TRAINING_EMAILS"] == "owner@heli.local"
