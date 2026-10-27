"""Activity 2: package onboarded LLMs as API Management products.

Renders every product definition in products/*.yaml into APIOps artifacts:
  * the product (subscription required, optional approval, per-developer limit)
  * product -> API links for the onboarded models it includes
  * product -> group links that control developer portal visibility
  * a product policy that enforces the token allowance per subscription key

Run Activity 1 first so the referenced model APIs exist.
Usage:  python 02-create-product/create_product.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import yaml

ACTIVITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = ACTIVITY_DIR.parent
PRODUCTS_DIR = ACTIVITY_DIR / "products"
TEMPLATES_DIR = ACTIVITY_DIR / "templates"
ARTIFACTS_DIR = REPO_ROOT / "apim-artifacts"

# Products whose names start with this prefix are owned by this script.
# Must match the prefix used by 01-onboard-llm for model APIs.
PREFIX = "llm-"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
REQUIRED_FIELDS = ("name", "displayName", "models", "limits")


def product_id(product: dict) -> str:
    return f"{PREFIX}{product['name']}"


def model_api_id(model_name: str) -> str:
    return f"{PREFIX}{model_name}"


def positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_products() -> list[dict]:
    products = []
    for path in sorted(PRODUCTS_DIR.glob("*.yaml")):
        product = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        missing = [field for field in REQUIRED_FIELDS if not product.get(field)]
        if missing:
            raise ValueError(f"{path.name}: missing {', '.join(missing)}")
        if not NAME_PATTERN.match(product["name"]):
            raise ValueError(f"{path.name}: name must be lowercase letters, digits and hyphens")

        limits = product["limits"]
        for limit in ("tokensPerMinute", "monthlyTokenQuota"):
            if not positive_int(limits.get(limit)):
                raise ValueError(f"{path.name}: limits.{limit} must be a positive integer")
        if not positive_int(product.get("subscriptionsLimit", 1)):
            raise ValueError(f"{path.name}: subscriptionsLimit must be a positive integer")

        for model_name in product["models"]:
            if not (ARTIFACTS_DIR / "apis" / model_api_id(model_name) / "apiInformation.json").exists():
                raise ValueError(
                    f"{path.name}: model '{model_name}' is not onboarded; run 01-onboard-llm/onboard_llm.py first"
                )
        products.append(product)

    if not products:
        raise ValueError(f"No product definitions found in {PRODUCTS_DIR}")
    return products


def render_product(product: dict) -> None:
    product_dir = ARTIFACTS_DIR / "products" / product_id(product)

    properties = {
        "displayName": product["displayName"],
        "description": product.get("description", ""),
        "subscriptionRequired": True,
        "approvalRequired": bool(product.get("approvalRequired", True)),
        "subscriptionsLimit": product.get("subscriptionsLimit", 1),
        "state": "published",
    }
    if product.get("terms"):
        properties["terms"] = product["terms"]
    write_json(product_dir / "productInformation.json", {"properties": properties})

    write_json(product_dir / "apis.json", [{"name": model_api_id(name)} for name in product["models"]])
    write_json(product_dir / "groups.json", [{"name": group} for group in product.get("visibleTo", ["developers"])])

    limits = product["limits"]
    policy = (TEMPLATES_DIR / "product-policy.xml").read_text(encoding="utf-8")
    policy = policy.replace("__TOKENS_PER_MINUTE__", str(limits["tokensPerMinute"]))
    policy = policy.replace("__MONTHLY_TOKEN_QUOTA__", str(limits["monthlyTokenQuota"]))
    write_text(product_dir / "policy.xml", policy)


def prune(directory: Path, keep: set[str]) -> None:
    """Remove artifacts for products that were deleted from the catalog."""
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_dir() and child.name.startswith(PREFIX) and child.name not in keep:
            shutil.rmtree(child)
            print(f"Removed stale artifact {child.relative_to(REPO_ROOT)}")


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, indent=2) + "\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    try:
        products = load_products()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for product in products:
        render_product(product)
    prune(ARTIFACTS_DIR / "products", {product_id(product) for product in products})

    print(f"Rendered {len(products)} product(s): {', '.join(product_id(product) for product in products)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
