"""Activity 3: publish the generated artifacts to API Management with the APIOps CLI.

Always previews with `apiops publish --dry-run` first. Never deletes resources that are
missing from the artifacts; retire models and products deliberately.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("publish")

REPO_ROOT = Path(__file__).resolve().parent


def run(artifacts: Path, subscription_id: str, resource_group: str, service_name: str, *, dry_run: bool) -> None:
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx not found; install Node.js 22+ and run 'npm ci'")

    # A relative source path avoids cmd.exe quoting problems with spaces on Windows.
    command = [
        npx, "--no-install", "apiops", "publish",
        "--subscription-id", subscription_id,
        "--resource-group", resource_group,
        "--service-name", service_name,
        "--source", artifacts.relative_to(REPO_ROOT).as_posix(),
    ]

    log.info("Previewing publish to %s", service_name)
    subprocess.run([*command, "--dry-run"], check=True, cwd=REPO_ROOT)
    if dry_run:
        return

    log.info("Publishing to %s", service_name)
    subprocess.run(command, check=True, cwd=REPO_ROOT)
