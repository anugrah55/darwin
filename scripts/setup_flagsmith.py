"""Bootstrap a local, self-hosted Flagsmith for Darwin (idempotent).

Creates an admin user, the "Darwin Lab" organisation, the "Darwin Demo" project and a "Production"
environment; mints an Organisation (Admin) API key for the Flagsmith CLI and a server-side SDK key for
ShopBot; connects the "Flagsmith database" warehouse so experiment events have somewhere to go; and
writes everything into .env.

Flagsmith Cloud users skip this script: create the project in the dashboard, connect a warehouse, and
put the same five values into .env yourself (see .env.example).

    uv run python scripts/setup_flagsmith.py [--api-url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "docker" / ".flagsmith-local.json"
ENV_FILE = ROOT / ".env"
BEGIN = "# --- flagsmith (written by scripts/setup_flagsmith.py) ---"
END = "# --- end flagsmith ---"

ADMIN_EMAIL = "admin@darwin.local"
ORG_NAME = "Darwin Lab"
PROJECT_NAME = "Darwin Demo"
ENV_NAME = "Production"


def fail(message: str) -> None:
    print(f"setup_flagsmith: {message}", file=sys.stderr)
    sys.exit(1)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATE_FILE.chmod(0o600)


def user_token(client: httpx.Client, state: dict) -> str:
    password = state.setdefault("admin_password", secrets.token_urlsafe(18))
    r = client.post(
        "/api/v1/auth/users/",
        json={
            "email": ADMIN_EMAIL,
            "password": password,
            "re_password": password,
            "first_name": "Darwin",
            "last_name": "Admin",
        },
    )
    if r.status_code == 201:
        print(f"  created admin user {ADMIN_EMAIL}")
        save_state(state)
        return r.json()["key"]
    r = client.post("/api/v1/auth/login/", json={"email": ADMIN_EMAIL, "password": password})
    if r.status_code != 200:
        fail(f"could not log in as {ADMIN_EMAIL}: {r.status_code} {r.text[:200]}")
    return r.json()["key"]


def find_or_create(client: httpx.Client, list_path: str, create_path: str, match: dict, body: dict) -> dict:
    r = client.get(list_path)
    r.raise_for_status()
    items = r.json()
    items = items.get("results", items) if isinstance(items, dict) else items
    for item in items:
        if all(item.get(k) == v for k, v in match.items()):
            return item
    r = client.post(create_path, json=body)
    if r.status_code not in (200, 201):
        fail(f"POST {create_path} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def write_env(values: dict[str, str]) -> None:
    block = "\n".join([BEGIN, *[f"{k}={v}" for k, v in values.items()], END])
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    text = pattern.sub(block, text) if pattern.search(text) else (text.rstrip("\n") + "\n\n" + block + "\n").lstrip("\n")
    ENV_FILE.write_text(text if text.endswith("\n") else text + "\n")
    ENV_FILE.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default="http://localhost:8000")
    args = parser.parse_args()
    api_url = args.api_url.rstrip("/")

    state = load_state()
    with httpx.Client(base_url=api_url, timeout=30) as anon:
        try:
            anon.get("/health/liveness/").raise_for_status()
        except httpx.HTTPError as exc:
            fail(f"Flagsmith is not reachable at {api_url} ({exc}). Start it with `make flagsmith-up`.")
        token = user_token(anon, state)

    with httpx.Client(base_url=api_url, timeout=30, headers={"Authorization": f"Token {token}"}) as fs:
        org = find_or_create(fs, "/api/v1/organisations/", "/api/v1/organisations/", {"name": ORG_NAME}, {"name": ORG_NAME})
        project = find_or_create(
            fs,
            f"/api/v1/projects/?organisation={org['id']}",
            "/api/v1/projects/",
            {"name": PROJECT_NAME},
            {"name": PROJECT_NAME, "organisation": org["id"]},
        )
        env = find_or_create(
            fs,
            f"/api/v1/environments/?project={project['id']}",
            "/api/v1/environments/",
            {"name": ENV_NAME},
            {"name": ENV_NAME, "project": project["id"]},
        )
        env_key = env["api_key"]
        print(f"  organisation {org['id']} · project {project['id']} · environment {ENV_NAME} ({env_key})")

        if not state.get("admin_api_key"):
            r = fs.post(f"/api/v1/organisations/{org['id']}/master-api-keys/", json={"name": "darwin-cli", "is_admin": True})
            if r.status_code not in (200, 201):
                fail(f"could not create the Organisation API key: {r.status_code} {r.text[:300]}")
            state["admin_api_key"] = r.json()["key"]
            save_state(state)
            print("  minted Organisation API key 'darwin-cli'")

        if not state.get("server_key") or state.get("server_key_env") != env_key:
            r = fs.post(f"/api/v1/environments/{env_key}/api-keys/", json={"name": "shopbot"})
            if r.status_code not in (200, 201):
                fail(f"could not create the server-side SDK key: {r.status_code} {r.text[:300]}")
            state["server_key"] = r.json()["key"]
            state["server_key_env"] = env_key
            save_state(state)
            print("  minted server-side SDK key 'shopbot'")

        r = fs.get(f"/api/v1/environments/{env_key}/warehouse-connections/?exclude_event_stats=true")
        r.raise_for_status()
        if not r.json():
            r = fs.post(
                f"/api/v1/environments/{env_key}/warehouse-connections/",
                json={"warehouse_type": "flagsmith", "name": "Flagsmith database"},
            )
            if r.status_code not in (200, 201):
                fail(f"could not connect the Flagsmith database warehouse: {r.status_code} {r.text[:300]}")
            print("  connected the 'Flagsmith database' warehouse")
        conn = fs.get(f"/api/v1/environments/{env_key}/warehouse-connections/?exclude_event_stats=true").json()[0]
        print(f"  warehouse: {conn['name']} — {conn['status']}")

    # The Flagsmith CLI scopes credentials by host: for anything but api.flagsmith.com it reads
    # FLAGSMITH_API_KEY_<host>_<port> and refuses a bare FLAGSMITH_API_KEY.
    scoped_key_var = "FLAGSMITH_API_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", httpx.URL(api_url).netloc.decode())
    write_env(
        {
            "FLAGSMITH_API_URL": api_url,
            scoped_key_var: state["admin_api_key"],
            "FLAGSMITH_PROJECT": str(project["id"]),
            "FLAGSMITH_ENVIRONMENT": env_key,
            "FLAGSMITH_SERVER_KEY": state["server_key"],
            "FLAGSMITH_EVENTS_URL": f"{api_url}/api/",
            "FLAGSMITH_DASHBOARD_URL": api_url,
        }
    )
    print(f"  wrote Flagsmith settings to {ENV_FILE.name}")
    print(f"\nDashboard: {api_url}  (log in as {ADMIN_EMAIL}; password in {STATE_FILE.relative_to(ROOT)})")


if __name__ == "__main__":
    main()
