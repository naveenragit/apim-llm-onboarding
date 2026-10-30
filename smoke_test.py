"""Activity 5: confirm each onboarded model answers through the APIM gateway.

Sends one small chat completion per model and expects HTTP 200. It uses APIM's built-in
all-access subscription, so it verifies routing and backend authentication, not product
token limits. The key is held in memory only.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from azure.core.exceptions import HttpResponseError
from azure.mgmt.apimanagement import ApiManagementClient

log = logging.getLogger("smoke_test")

API_VERSION = "2024-10-21"
ALL_ACCESS_SUBSCRIPTION = "master"


def run(config: dict, client: ApiManagementClient, resource_group: str, service_name: str) -> None:
    try:
        gateway = client.api_management_service.get(resource_group, service_name).gateway_url
        key = client.subscription.list_secrets(resource_group, service_name, ALL_ACCESS_SUBSCRIPTION).primary_key
    except HttpResponseError as error:
        raise RuntimeError(f"could not read the APIM gateway or all-access key: {error.message}") from None

    body = json.dumps({"messages": [{"role": "user", "content": "Reply with OK."}]}).encode()
    failures = []
    for model in config.get("models") or []:
        url = (
            f"{gateway}/llm/{model['name']}/openai/deployments/{model['deployment']}"
            f"/chat/completions?api-version={API_VERSION}"
        )
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"api-key": key, "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except urllib.error.URLError as error:
            status = error.reason
        log.info("%s -> %s", model["name"], status)
        if status != 200:
            failures.append(f"{model['name']} ({status})")

    if failures:
        raise RuntimeError(f"smoke test failed for {', '.join(failures)}")
    log.info("Smoke test passed for %d model(s)", len(config.get("models") or []))
