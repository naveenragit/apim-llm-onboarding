"""Activity 5: confirm each product serves its models through the APIM gateway.

For every product, uses a dedicated ownerless release-check subscription scoped to that
product, so calls go through the same product policies as developer calls. Each model in
the product must return HTTP 200 with the token-limit headers. Keys are held in memory only,
and the primary key is regenerated after each run so it never becomes a standing credential.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import SubscriptionCreateParameters

from create_product import product_id, release_check_id
from onboard_model import api_id

log = logging.getLogger("smoke_test")

API_VERSION = "2024-10-21"
TOKEN_HEADERS = ("x-ratelimit-remaining-tokens", "x-quota-remaining-tokens")
# A new or regenerated subscription key took ~10s to reach the gateway in testing.
KEY_PROPAGATION_RETRIES = 12
KEY_PROPAGATION_DELAY = 5


def release_check_key(client: ApiManagementClient, resource_group: str, service_name: str, product: str) -> str:
    scope = (resource_group, service_name)
    name = release_check_id(product)
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
    """Returns None on success, otherwise the reason for failure. Retries 401 while a new key propagates."""
    for attempt in range(KEY_PROPAGATION_RETRIES):
        reason = _call_model_once(gateway, key, model)
        if reason != "HTTP 401" or attempt == KEY_PROPAGATION_RETRIES - 1:
            return reason
        time.sleep(KEY_PROPAGATION_DELAY)


def _call_model_once(gateway: str, key: str, model: dict) -> str | None:
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


def run(
    config: dict, client: ApiManagementClient, resource_group: str, service_name: str, gateway_url: str | None = None
) -> None:
    """gateway_url overrides the instance's default gateway, e.g. a private hostname."""
    models = {model["name"]: model for model in config.get("models") or []}
    try:
        gateway = (gateway_url or client.api_management_service.get(resource_group, service_name).gateway_url).rstrip("/")
    except HttpResponseError as error:
        raise RuntimeError(f"could not read the APIM gateway: {error.message}") from None

    failures, checks = [], 0
    for product in config.get("products") or []:
        product_name = product_id(product["name"])
        try:
            key = release_check_key(client, resource_group, service_name, product_name)
        except HttpResponseError as error:
            raise RuntimeError(f"could not prepare the release-check subscription for {product_name}: {error.message}") from None
        try:
            for model_name in product["models"]:
                reason = call_model(gateway, key, models[model_name])
                checks += 1
                log.info("%s / %s -> %s", product_name, api_id(model_name), reason or "OK")
                if reason:
                    failures.append(f"{product_name}/{api_id(model_name)} ({reason})")
        finally:
            try:
                client.subscription.regenerate_primary_key(resource_group, service_name, release_check_id(product_name))
            except HttpResponseError as error:
                failures.append(f"{product_name} (could not regenerate the release-check key: {error.message})")

    if failures:
        raise RuntimeError(f"smoke test failed for {', '.join(failures)}")
    log.info("Smoke test passed for %d product/model check(s)", checks)
