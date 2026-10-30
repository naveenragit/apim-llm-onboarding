"""Onboard LLMs into Azure API Management and grant developers access.

Runs the activities in order for one environment:
  onboard  - generate the API and backend for each model           (onboard_model.py)
  product  - generate products that bundle models with token limits (create_product.py)
  publish  - deploy the generated artifacts with the APIOps CLI     (publish.py)
  access   - create developer users and product subscriptions       (grant_access.py)
  smoke    - call each model once through the APIM gateway          (smoke_test.py)

The whole config.yaml is validated before any activity runs.

Examples:
  python main.py --env dev                            # all activities
  python main.py --env dev --steps onboard product    # validate and generate artifacts only (no Azure calls)
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
import grant_access
import onboard_model
import publish
import smoke_test

REPO_ROOT = Path(__file__).resolve().parent
STEPS = ("onboard", "product", "publish", "access", "smoke")
AZURE_STEPS = {"publish", "access", "smoke"}

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

    if AZURE_STEPS & set(args.steps):
        for option in ("subscription_id", "resource_group", "service_name"):
            if not getattr(args, option):
                parser.error(f"--{option.replace('_', '-')} is required for publish/access/smoke (or set its environment variable)")
    return args


def validate_config(config: dict, environment: str) -> None:
    """Fails on any invalid model, product, or grant before Azure is touched."""
    models = config.get("models") or []
    onboard_model.validate(models, environment)
    create_product.validate(config.get("products") or [], {model["name"] for model in models})
    grant_access.load_grants(config, environment)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)

    artifacts = REPO_ROOT / "build" / args.env / "apim-artifacts"
    steps = [step for step in STEPS if step in args.steps]

    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
        validate_config(config, args.env)
        if "onboard" in steps:
            onboard_model.run(config, args.env, artifacts)
        if "product" in steps:
            create_product.run(config, artifacts)
        if "publish" in steps:
            if not artifacts.exists():
                raise ValueError(f"{artifacts.relative_to(REPO_ROOT)} not found; run the onboard and product steps first")
            publish.run(artifacts, args.subscription_id, args.resource_group, args.service_name, dry_run=args.dry_run)
        if "access" in steps and run_access(config, args):
            return 1
        if "smoke" in steps:
            if args.dry_run:
                log.info("[dry-run] skipping smoke test")
            else:
                run_smoke(config, args)
    except (OSError, yaml.YAMLError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        log.error("%s", error)
        return 1
    return 0


def api_client(args: argparse.Namespace):
    # Imported lazily so offline generation does not need Azure credentials.
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.apimanagement import ApiManagementClient

    credential = DefaultAzureCredential()
    return credential, ApiManagementClient(credential, args.subscription_id)


def run_access(config: dict, args: argparse.Namespace) -> int:
    from azure.keyvault.secrets import SecretClient

    credential, client = api_client(args)
    return grant_access.run(
        config,
        args.env,
        client,
        args.resource_group,
        args.service_name,
        secrets=SecretClient(args.key_vault_url, credential) if args.key_vault_url else None,
        notify=args.notify,
        dry_run=args.dry_run,
    )


def run_smoke(config: dict, args: argparse.Namespace) -> None:
    _, client = api_client(args)
    smoke_test.run(config, client, args.resource_group, args.service_name)


if __name__ == "__main__":
    sys.exit(main())
