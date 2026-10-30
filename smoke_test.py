"""Activity 5: confirm each product serves its models through the APIM gateway.

For every product, uses a dedicated ownerless release-check subscription scoped to that
product, so calls go through the same product policies as developer calls. Each model in
the product must return HTTP 200 with the token-limit headers. Keys are held in memory only.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import SubscriptionCreateParameters

from create_product import product_id
from onboard_model import api_id

log = logging.getLogger("smoke_test")

API_VERSION = "2024-10-21"
TOKEN_HEADERS = ("x-ratelimit-remaining-tokens", "x-quota-remaining-tokens")


def release_check_key(client: ApiManagementClient, resource_group: str, service_name: str, product: str) -> str:
    scope = (resource_group, service_name)
    name = f"{product}--release-check"
    try:
        subscription = client.subscription.get(*scope, name)
    except ResourceNotFoundError:
        product_arm_id = client.product.get(*scope, product).id
        client.subscription.create_or_update(
            *scope,
            name,
            SubscriptionCreateParameters(scope=product_arm_id, display_name=f"{product} release check", state="active"),
        )
        log.info("Created release-check subscription %s", name)
    else:
        if subscription.state != "active":
            raise RuntimeError(f"release-check subscription {name} is {subscription.state}; reactivate it to run the smoke test")
    return client.subscription.list_secrets(*scope, name).primary_key


def call_model(gateway: str, key: str, model: dict) -> str | None:
    """Returns None on success, otherwise the reason for failure."""
    url = (
        f"{gateway}/llm/{model['name']}/openai/deployments/{model['deployment']}"
        f"/chat/completions?api-version={API_VERSION}"
    )
    body = json.dumps({"messages": [{"role": "user", "content": "Reply with OK."}]}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"api-key": key, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                return f"HTTP {response.status}"
            missing = [header for header in TOKEN_HEADERS if response.headers.get(header) is None]
            return f"missing headers {', '.join(missing)}" if missing else None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except urllib.error.URLError as error:
        return str(error.reason)


def run(config: dict, client: ApiManagementClient, resource_group: str, service_name: str) -> None:
    models = {model["name"]: model for model in config.get("models") or []}
    try:
        gateway = client.api_management_service.get(resource_group, service_name).gateway_url
    except HttpResponseError as error:
        raise RuntimeError(f"could not read the APIM gateway: {error.message}") from None

    failures, checks = [], 0
    for product in config.get("products") or []:
        product_name = product_id(product["name"])
        try:
            key = release_check_key(client, resource_group, service_name, product_name)
        except HttpResponseError as error:
            raise RuntimeError(f"could not prepare the release-check subscription for {product_name}: {error.message}") from None
        for model_name in product["models"]:
            reason = call_model(gateway, key, models[model_name])
            checks += 1
            log.info("%s / %s -> %s", product_name, api_id(model_name), reason or "OK")
            if reason:
                failures.append(f"{product_name}/{api_id(model_name)} ({reason})")

    if failures:
        raise RuntimeError(f"smoke test failed for {', '.join(failures)}")
    log.info("Smoke test passed for %d product/model check(s)", checks)
