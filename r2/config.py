from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODELS = ROOT / "models"
SETTINGS = DATA / "settings.json"
DEFAULTS = {
    "ha_url": "http://homeassistant.local:8123",
    "allowed_entities": [],
    "allowed_locks": [],
    "lock_names": {},
    "wake_threshold": 0.65,
    "wake_validated": False,
    "idle_seconds": 30,
    "max_seconds": 300,
}


def read_settings() -> dict:
    return DEFAULTS | (json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {})


def save_settings(changes: dict) -> dict:
    settings = read_settings() | changes
    DATA.mkdir(exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS)
    return settings


def validate_ha_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use the Home Assistant origin, such as http://homeassistant.local:8123")
    # This installation is deliberately limited to the user's local HA instance.
    import ipaddress
    host = parsed.hostname
    local = host in {"localhost", "homeassistant.local"} or host.endswith(".local")
    try:
        addr = ipaddress.ip_address(host)
        local = addr.is_private and not addr.is_unspecified
    except ValueError:
        pass
    if not local:
        raise ValueError("Home Assistant must use a local hostname or private network address.")
    return value.rstrip("/")


class Credentials:
    """Never fall back to a plaintext keyring or browser storage."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("This app uses Windows Credential Manager.")
        from keyring.backends.Windows import WinVaultKeyring
        self.vault = WinVaultKeyring()

    def get(self, name: str) -> str | None:
        return self.vault.get_password("R2 Voice Assistant", name)

    def set(self, name: str, value: str):
        if name not in {"openai_api_key", "ha_token"}:
            raise ValueError("Unknown credential")
        self.vault.set_password("R2 Voice Assistant", name, value)
