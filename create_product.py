"""Activity 2: package onboarded models as API Management products.

For each product in config.yaml, writes APIOps artifacts:
  * the product (subscription and approval required, subscriptions per developer)
  * links to the model APIs it includes and the groups that can see it in the developer portal
  * a policy that caps each request's max_tokens and enforces the token allowance per subscription
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from onboard_model import PREFIX, api_id, write_json, write_text

log = logging.getLogger("create_product")

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
# Anonymous portal visitors belong to this built-in group.
BLOCKED_GROUPS = {"guests"}

PRODUCT_POLICY = """<policies>
    <inbound>
        <base />
        <!-- The token limit only blocks after usage is reported, so cap each request's completion size up front. -->
        <choose>
            <when condition="@{
                var body = context.Request.Body?.As<JObject>(preserveContent: true);
                var requested = new[] { body?["max_tokens"], body?["max_completion_tokens"] };
                return requested.Where(t => t != null).Where(t => t.Type != JTokenType.Null)
                    .Any(t => t.Type != JTokenType.Integer || (long)t > __MAX_TOKENS_PER_REQUEST__);
            }">
                <return-response>
                    <set-status code="400" reason="max_tokens exceeds __MAX_TOKENS_PER_REQUEST__" />
                </return-response>
            </when>
        </choose>
        <!-- Token allowance per subscription, shared by its keys and every model in the product. -->
        <llm-token-limit counter-key="@(context.Subscription.Id)"
                         tokens-per-minute="__TOKENS_PER_MINUTE__"
                         token-quota="__MONTHLY_TOKEN_QUOTA__"
                         token-quota-period="Monthly"
                         estimate-prompt-tokens="true"
                         remaining-tokens-header-name="x-ratelimit-remaining-tokens"
                         remaining-quota-tokens-header-name="x-quota-remaining-tokens" />
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>
"""


def product_id(product_name: str) -> str:
    return f"{PREFIX}{product_name}"


def release_check_id(product: str) -> str:
    """Subscription the smoke test uses for a product id such as 'llm-gpt-4o-standard'."""
    return f"{product}--release-check"


def positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(products: list[dict], model_names: set[str]) -> None:
    if not products:
        raise ValueError("config.yaml: no products defined")
    seen = set()
    for product in products:
        where = f"product '{product.get('name', '?')}'"
        missing = [field for field in ("name", "displayName", "models", "limits") if not product.get(field)]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if not NAME_PATTERN.match(product["name"]):
            raise ValueError(f"{where}: name must be lowercase letters, digits and hyphens")
        if product["name"] in seen:
            raise ValueError(f"{where}: defined more than once")
        for limit in ("tokensPerMinute", "monthlyTokenQuota", "maxTokensPerRequest"):
            if not positive_int(product["limits"].get(limit)):
                raise ValueError(f"{where}: limits.{limit} must be a positive integer")
        if not positive_int(product.get("subscriptionsLimit", 1)):
            raise ValueError(f"{where}: subscriptionsLimit must be a positive integer")
        if product.get("approvalRequired", True) is not True:
            raise ValueError(f"{where}: approvalRequired must be true; grant access through config.yaml")
        groups = product.get("visibleTo", ["developers"])
        if not isinstance(groups, list) or not all(isinstance(g, str) and GROUP_PATTERN.match(g) for g in groups):
            raise ValueError(f"{where}: visibleTo must be a list of APIM group ids")
        blocked = [group for group in groups if group.lower() in BLOCKED_GROUPS]
        if blocked:
            raise ValueError(f"{where}: visibleTo may not include {', '.join(blocked)}")
        unknown = [name for name in product["models"] if name not in model_names]
        if unknown:
            raise ValueError(f"{where}: unknown model(s) {', '.join(unknown)}")
        seen.add(product["name"])


def run(config: dict, artifacts: Path) -> None:
    products = config.get("products") or []
    validate(products, {model["name"] for model in config.get("models") or []})
    shutil.rmtree(artifacts / "products", ignore_errors=True)

    for product in products:
        product_dir = artifacts / "products" / product_id(product["name"])
        properties = {
            "displayName": product["displayName"],
            "description": product.get("description", ""),
            "subscriptionRequired": True,
            "approvalRequired": True,
            "subscriptionsLimit": product.get("subscriptionsLimit", 1),
            "state": "published",
        }
        if product.get("terms"):
            properties["terms"] = product["terms"]
        write_json(product_dir / "productInformation.json", {"properties": properties})
        write_json(product_dir / "apis.json", [{"name": api_id(name)} for name in product["models"]])
        write_json(product_dir / "groups.json", [{"name": group} for group in product.get("visibleTo", ["developers"])])

        limits = product["limits"]
        policy = PRODUCT_POLICY
        for placeholder, value in (
            ("__TOKENS_PER_MINUTE__", limits["tokensPerMinute"]),
            ("__MONTHLY_TOKEN_QUOTA__", limits["monthlyTokenQuota"]),
            ("__MAX_TOKENS_PER_REQUEST__", limits["maxTokensPerRequest"]),
        ):
            policy = policy.replace(placeholder, str(value))
        write_text(product_dir / "policy.xml", policy)

    log.info("Created %d product(s): %s", len(products), ", ".join(product_id(p["name"]) for p in products))
