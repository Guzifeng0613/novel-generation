"""Application configuration.

Configuration is intentionally read from environment variables without a
``pydantic-settings`` dependency.  Secrets remain server-side and are exposed
as ``SecretStr`` values so an accidental ``model_dump`` only contains masked
representations.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # fail early instead of silently using a port/budget
        raise ValueError(f"{name} must be an integer") from exc


class Settings(BaseModel):
    """Runtime settings for the local, single-user service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_name: str = "小说续写编辑部"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = WORKSPACE_ROOT / "data"
    env_file: Path = WORKSPACE_ROOT / ".env"
    database_url_override: str | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    max_archive_members: int = Field(default=5_000, gt=0)

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        database_path = (self.data_dir / "novel_orchestrator.sqlite3").resolve()
        return f"sqlite:///{database_path.as_posix()}"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def project_dir(self) -> Path:
        return self.data_dir / "projects"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = Path(os.getenv("NOVEL_ENV_FILE", str(WORKSPACE_ROOT / ".env"))).expanduser()
    load_dotenv(env_file, override=False)
    data_dir = Path(os.getenv("NOVEL_DATA_DIR", str(WORKSPACE_ROOT / "data"))).expanduser()
    return Settings(
        host=os.getenv("NOVEL_HOST", "127.0.0.1"),
        port=_env_int("NOVEL_PORT", 8000),
        data_dir=data_dir,
        env_file=env_file,
        database_url_override=os.getenv("NOVEL_DATABASE_URL"),
        openai_api_key=(SecretStr(value) if (value := os.getenv("OPENAI_API_KEY")) else None),
        deepseek_api_key=(SecretStr(value) if (value := os.getenv("DEEPSEEK_API_KEY")) else None),
        max_upload_bytes=_env_int("NOVEL_MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
        max_archive_members=_env_int("NOVEL_MAX_ARCHIVE_MEMBERS", 5_000),
    )


def reset_settings_cache() -> None:
    """Test helper for code that changes environment variables."""

    get_settings.cache_clear()


def save_provider_api_keys(
    settings: Settings,
    *,
    openai_api_key: str | None = None,
    deepseek_api_key: str | None = None,
) -> Settings:
    """Persist provider keys and return the corresponding in-memory settings."""

    updates = {
        "OPENAI_API_KEY": openai_api_key,
        "DEEPSEEK_API_KEY": deepseek_api_key,
    }
    changed = {name: value.strip() for name, value in updates.items() if value is not None}
    if not changed:
        raise ValueError("至少需要提供一个 API 密钥")
    if any(not value or any(character in value for character in "\r\n\x00") for value in changed.values()):
        raise ValueError("API 密钥不能为空，也不能包含换行或空字符")

    path = settings.env_file.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = set(changed)
    rendered: list[str] = []
    for line in lines:
        name, separator, _value = line.partition("=")
        key = name.strip()
        if separator and key in changed:
            rendered.append(f"{key}={json.dumps(changed[key])}")
            remaining.discard(key)
        else:
            rendered.append(line)
    rendered.extend(f"{key}={json.dumps(changed[key])}" for key in sorted(remaining))
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")

    field_updates = {
        "openai_api_key": SecretStr(changed["OPENAI_API_KEY"])
        if "OPENAI_API_KEY" in changed
        else settings.openai_api_key,
        "deepseek_api_key": SecretStr(changed["DEEPSEEK_API_KEY"])
        if "DEEPSEEK_API_KEY" in changed
        else settings.deepseek_api_key,
    }
    return settings.model_copy(update=field_updates)
