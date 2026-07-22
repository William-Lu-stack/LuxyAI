"""Verify dynamic bearer-token onboarding without executing kubeconfig plugins."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.services.cluster_registry import ClusterRegistry  # noqa: E402


def main() -> None:
    kubeconfig_path = Path(os.environ["E2E_KUBECONFIG"]).resolve()
    tokens = [line.strip() for line in sys.stdin if line.strip()]
    if len(tokens) != 2:
        raise AssertionError("expected two short-lived bearer tokens on stdin")
    state_dir = Path(tempfile.mkdtemp(prefix="flawless-token-e2e-"))
    try:
        payload = yaml.safe_load(kubeconfig_path.read_text(encoding="utf-8"))
        payload["users"][0]["user"] = {
            "exec": {
                "apiVersion": "client.authentication.k8s.io/v1",
                "command": "this-command-must-never-run",
            },
        }
        dynamic_config = yaml.safe_dump(payload, sort_keys=False)
        registry = ClusterRegistry(state_dir / "clusters.db")
        saved = registry.save_verified(
            content=dynamic_config,
            name="dynamic-token-e2e",
            cluster_id="kube-dynamic-e2e",
            bearer_token=tokens[0],
        )
        assert saved["status"] == "connected", saved
        refreshed = registry.refresh_token(saved["id"], tokens[1])
        assert refreshed["status"] == "connected", refreshed
        database = (state_dir / "clusters.db").read_bytes()
        assert tokens[0].encode() not in database
        assert tokens[1].encode() not in database
        assert b"this-command-must-never-run" not in database
        print({
            "dynamic_token_onboarding": True,
            "dynamic_token_refresh": True,
            "exec_plugin_executed": False,
            "plaintext_credentials_persisted": False,
        })
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
