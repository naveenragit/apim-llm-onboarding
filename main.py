"""Onboard LLMs into Azure API Management and grant developers access.

Runs the activities in order for one environment:
  onboard  - generate the API and backend for each model           (onboard_model.py)
  product  - generate products that bundle models with token limits (create_product.py)
  publish  - deploy the generated artifacts with the APIOps CLI     (publish.py)
  access   - create developer users and product subscriptions       (grant_access.py)

Examples:
  python main.py --env dev                            # all four activities
  python main.py --env dev --steps onboard product    # generate artifacts only (no Azure calls)
  python main.py --env dev --steps access --dry-run   # preview access changes

Azure settings come from flags or environment variables:
  AZURE_SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_SERVICE_NAME, ACCESS_KEY_VAULT_URL (optional)
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

import create_product
import onboard_model
import publish

REPO_ROOT = Path(__file__).resolve().parent
STEPS = ("onboard", "product", "publish", "access")

log = logging.getLogger("main")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help="Environment name defined in config.yaml, e.g. dev")
    parser.add_argument("--steps", nargs="+", choices=STEPS, default=list(STEPS), help="Activities to run (default: all)")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.yaml")
    parser.add_argument("--subscription-id", default=os.environ.get("AZURE_SUBSCRIPTION_ID"))
    parser.add_argument("--resource-group", default=os.environ.get("APIM_RESOURCE_GROUP"))
    parser.add_argument("--service-name", default=os.environ.get("APIM_SERVICE_NAME"))
    parser.add_argument("--key-vault-url", default=os.environ.get("ACCESS_KEY_VAULT_URL") or None,
                        help="Optional Key Vault that receives each newly activated subscription key")
    parser.add_argument("--notify", action="store_true", help="Let APIM email developers about access changes")
    parser.add_argument("--dry-run", action="store_true", help="Preview publish and access changes without applying them")
    args = parser.parse_args(argv)

    if {"publish", "access"} & set(args.steps):
        for option in ("subscription_id", "resource_group", "service_name"):
            if not getattr(args, option):
                parser.error(f"--{option.replace('_', '-')} is required for publish/access (or set its environment variable)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)

    config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    artifacts = REPO_ROOT / "build" / args.env / "apim-artifacts"
    steps = [step for step in STEPS if step in args.steps]

    try:
        if "onboard" in steps:
            onboard_model.run(config, args.env, artifacts)
        if "product" in steps:
            create_product.run(config, artifacts)
        if "publish" in steps:
            if not artifacts.exists():
                raise ValueError(f"{artifacts.relative_to(REPO_ROOT)} not found; run the onboard and product steps first")
            publish.run(artifacts, args.subscription_id, args.resource_group, args.service_name, dry_run=args.dry_run)
        if "access" in steps:
            return 1 if run_access(config, args) else 0
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        log.error("%s", error)
        return 1
    return 0


def run_access(config: dict, args: argparse.Namespace) -> int:
    # Imported lazily so offline generation does not need the Azure SDKs.
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    from azure.mgmt.apimanagement import ApiManagementClient

    import grant_access

    credential = DefaultAzureCredential()
    return grant_access.run(
        config,
        args.env,
        ApiManagementClient(credential, args.subscription_id),
        args.resource_group,
        args.service_name,
        secrets=SecretClient(args.key_vault_url, credential) if args.key_vault_url else None,
        notify=args.notify,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
