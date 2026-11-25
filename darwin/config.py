"""Settings, read from the environment (and a local .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """Load KEY=VALUE lines from .env into os.environ. Variables that are already set win."""
    path = path or Path(os.environ.get("DARWIN_ENV_FILE", ROOT / ".env"))
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def scoped_key_var(api_url: str) -> str:
    """The Flagsmith CLI reads FLAGSMITH_API_KEY only for api.flagsmith.com; other hosts use
    FLAGSMITH_API_KEY_<host>_<port> (for example FLAGSMITH_API_KEY_localhost_8000)."""
    netloc = re.sub(r"^https?://", "", api_url).split("/")[0]
    return "FLAGSMITH_API_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", netloc)


def _is_cloud(api_url: str) -> bool:
    return re.sub(r"^https?://", "", api_url).rstrip("/") == "api.flagsmith.com"


@dataclass(frozen=True)
class Settings:
    flag: str
    data_dir: Path
    flagsmith_api_url: str
    flagsmith_admin_key: str | None
    flagsmith_project: str | None
    flagsmith_environment: str | None
    flagsmith_server_key: str | None
    flagsmith_sdk_api_url: str | None
    flagsmith_events_url: str | None
    flagsmith_dashboard_url: str
    budget_usd: float
    coach_model: str
    shopbot_url: str
    lab_host: str
    lab_port: int
    notebook_path: Path

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        env = os.environ.get
        api_url = env("FLAGSMITH_API_URL", "https://api.flagsmith.com").rstrip("/")
        admin_key = env("FLAGSMITH_ADMIN_API_KEY") or env(scoped_key_var(api_url))
        if not admin_key and _is_cloud(api_url):
            admin_key = env("FLAGSMITH_API_KEY")
        sdk_url = env("FLAGSMITH_SDK_API_URL")
        if not sdk_url and not _is_cloud(api_url):
            sdk_url = f"{api_url}/api/v1/"
        events_url = env("FLAGSMITH_EVENTS_URL")
        if not events_url and not _is_cloud(api_url):
            events_url = f"{api_url}/api/"
        dashboard = env("FLAGSMITH_DASHBOARD_URL") or ("https://app.flagsmith.com" if _is_cloud(api_url) else api_url)
        data_dir = Path(env("DARWIN_DATA_DIR", str(ROOT / "data")))
        return cls(
            flag=env("DARWIN_FLAG", "shopbot_brain"),
            data_dir=data_dir,
            flagsmith_api_url=api_url,
            flagsmith_admin_key=admin_key,
            flagsmith_project=env("FLAGSMITH_PROJECT"),
            flagsmith_environment=env("FLAGSMITH_ENVIRONMENT"),
            flagsmith_server_key=env("FLAGSMITH_SERVER_KEY"),
            flagsmith_sdk_api_url=sdk_url,
            flagsmith_events_url=events_url,
            flagsmith_dashboard_url=dashboard.rstrip("/"),
            budget_usd=float(env("DARWIN_BUDGET_USD", "2.00")),
            coach_model=env("DARWIN_COACH_MODEL", "claude-opus-5-5"),
            shopbot_url=env("SHOPBOT_URL", "http://127.0.0.1:8100").rstrip("/"),
            lab_host=env("DARWIN_LAB_HOST", "127.0.0.1"),
            lab_port=int(env("DARWIN_LAB_PORT", "8200")),
            notebook_path=Path(env("DARWIN_NOTEBOOK", str(ROOT / "LAB_NOTEBOOK.md"))),
        )

    def cli_env(self) -> dict[str, str]:
        """Environment for `flagsmith` subprocesses: credentials scoped the way the CLI expects."""
        out = {k: v for k, v in os.environ.items() if not k.startswith("FLAGSMITH_API_KEY")}
        out["FLAGSMITH_API_URL"] = self.flagsmith_api_url
        out["FLAGSMITH_NO_INPUT"] = "1"
        if self.flagsmith_admin_key:
            var = "FLAGSMITH_API_KEY" if _is_cloud(self.flagsmith_api_url) else scoped_key_var(self.flagsmith_api_url)
            out[var] = self.flagsmith_admin_key
        if self.flagsmith_project:
            out["FLAGSMITH_PROJECT"] = self.flagsmith_project
        if self.flagsmith_environment:
            out["FLAGSMITH_ENVIRONMENT"] = self.flagsmith_environment
        return out

    def missing_for_admin(self) -> list[str]:
        missing = []
        if not self.flagsmith_admin_key:
            missing.append(f"FLAGSMITH_API_KEY (or {scoped_key_var(self.flagsmith_api_url)})")
        if not self.flagsmith_project:
            missing.append("FLAGSMITH_PROJECT")
        if not self.flagsmith_environment:
            missing.append("FLAGSMITH_ENVIRONMENT")
        return missing

    def flag_url(self) -> str:
        return f"{self.flagsmith_dashboard_url}/project/{self.flagsmith_project}/environment/{self.flagsmith_environment}/features"
