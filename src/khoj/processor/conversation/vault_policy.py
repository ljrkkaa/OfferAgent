import json
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

POLICY_PATHS = (
    ".offeragent/vault-policy.json",
    ".offeragent/vault-policy.yaml",
    ".offeragent/vault-policy.yml",
)

GENERIC_VAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "routes": {
        "notes": {
            "description": "Read, create, append, or edit files in the user's knowledge base.",
        }
    },
    "write_safety": {
        "default_mode": "client_action",
        "require_confirmation_for_overwrite": True,
        "allow_create_extensions": [".md", ".txt"],
    },
}


def load_vault_policy(root: Optional[Path]) -> dict[str, Any]:
    if root is None:
        return GENERIC_VAULT_POLICY.copy()

    safe_root = root.resolve(strict=False)
    for relpath in POLICY_PATHS:
        path = root / relpath
        if not path.is_file():
            continue
        try:
            safe_path = path.resolve(strict=True)
            if not safe_path.is_relative_to(safe_root):
                continue
            text = safe_path.read_text(encoding="utf-8")
            if path.suffix.lower() == ".json":
                policy = json.loads(text)
            else:
                policy = yaml.safe_load(text)
        except Exception as exc:
            logger.warning("Failed to load OfferAgent vault policy from %s: %s", path, exc)
            continue
        if isinstance(policy, dict):
            return policy

    return GENERIC_VAULT_POLICY.copy()


def compact_policy_for_prompt(policy: dict[str, Any], limit: int = 6000) -> str:
    try:
        text = json.dumps(policy, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        text = json.dumps(GENERIC_VAULT_POLICY, ensure_ascii=False, indent=2)
    return text[:limit]
