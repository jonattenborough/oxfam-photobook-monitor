"""Store existing Production application credentials locally without an API call."""
from __future__ import annotations

import getpass
import json
import tomllib

from .config import load_config


def setup() -> None:
    config = load_config()
    path = config.data_dir / "secrets.toml"
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise PermissionError("Existing secrets file must be owner-only (chmod 600)")
        if "ebay" in tomllib.loads(path.read_text()):
            raise RuntimeError("eBay credentials are already stored. Edit the private secrets file if rotating keys")
    print("Enter the Production App ID and Cert ID from eBay Developer Program > Application Keys.")
    client_id = getpass.getpass("Production App ID / Client ID (hidden): ").strip()
    client_secret = getpass.getpass("Production Cert ID / Client Secret (hidden): ").strip()
    if len(client_id) < 10 or len(client_secret) < 10 or any(ch.isspace() for ch in client_id + client_secret):
        raise ValueError("Both Production credentials are required; nothing saved")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    contents = "\n[ebay]\nclient_id = " + json.dumps(client_id) + "\nclient_secret = " + json.dumps(client_secret) + "\n"
    if path.exists():
        with path.open("a", encoding="utf-8") as stream:
            stream.write(contents)
    else:
        path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    print("Production keys stored locally. No eBay request was made; scanning remains off in shadow mode.")
