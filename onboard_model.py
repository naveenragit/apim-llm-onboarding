"""Activity 1: onboard LLM deployments as API Management APIs.

For each model in config.yaml, writes APIOps artifacts for one environment:
  * a backend pointing at the model resource endpoint
  * an API (OpenAPI spec + policy) that only routes to the onboarded deployment and
    authenticates to the model with APIM's managed identity
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger("onboard_model")

PREFIX = "llm-"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# APIM sends its managed-identity token to this host, so only Azure AI resource endpoints are accepted.
ENDPOINT_PATTERN = re.compile(
    r"^https://[a-z0-9][a-z0-9-]{0,62}\.(openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)$"
)

API_POLICY = """<policies>
    <inbound>
        <!-- URLs end up in logs, so the subscription key is accepted only in the api-key header. -->
        <choose>
            <when condition="@(context.Request.Url.Query.ContainsKey("subscription-key"))">
                <return-response>
                    <set-status code="400" reason="Send the subscription key in the api-key header" />
                </return-response>
            </when>
            <when condition="@(context.Request.MatchedParameters.GetValueOrDefault("deployment-id", "") != "__DEPLOYMENT__")">
                <return-response>
                    <set-status code="404" reason="Deployment not available" />
                </return-response>
            </when>
        </choose>
        <base />
        <set-backend-service backend-id="__BACKEND_ID__" />
        <!-- Callers authenticate with their APIM key; APIM authenticates to the model with its managed identity. -->
        <set-header name="api-key" exists-action="delete" />
        <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
        <llm-emit-token-metric namespace="llm-gateway">
            <dimension name="API ID" />
            <dimension name="Product ID" />
            <dimension name="Subscription ID" />
        </llm-emit-token-metric>
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

# Minimal Azure OpenAI chat completions contract; extend it to expose more operations.
OPENAPI_SPEC = {
    "openapi": "3.0.1",
    "info": {
        "title": "LLM",
        "description": "Azure OpenAI chat completions exposed through API Management.",
        "version": "2024-10-21",
    },
    "paths": {
        "/deployments/{deployment-id}/chat/completions": {
            "post": {
                "operationId": "ChatCompletions_Create",
                "summary": "Creates a completion for the chat message.",
                "parameters": [
                    {"name": "deployment-id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {
                        "name": "api-version",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "example": "2024-10-21"},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/ChatCompletionRequest"}}
                    },
                },
                "responses": {
                    "200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}
                },
            }
        }
    },
    "components": {
        "schemas": {
            "ChatCompletionRequest": {
                "type": "object",
                "required": ["messages"],
                "properties": {"messages": {"type": "array", "items": {"type": "object"}}},
                "additionalProperties": True,
            }
        }
    },
}


def api_id(model_name: str) -> str:
    return f"{PREFIX}{model_name}"


def backend_id(model_name: str) -> str:
    return f"{PREFIX}{model_name}-backend"


def normalize_endpoint(endpoint: object) -> str:
    return str(endpoint).strip().lower().rstrip("/")


def validate(models: list[dict], environment: str, allowed_endpoints: set[str] | None = None) -> None:
    """allowed_endpoints, when given, is the exact set of model endpoints this environment may use."""
    if not models:
        raise ValueError("config.yaml: no models defined")
    seen = set()
    for model in models:
        where = f"model '{model.get('name', '?')}'"
        missing = [field for field in ("name", "displayName", "deployment", "endpoints") if not model.get(field)]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if not NAME_PATTERN.match(model["name"]):
            raise ValueError(f"{where}: name must be lowercase letters, digits and hyphens")
        if model["name"] in seen:
            raise ValueError(f"{where}: defined more than once")
        if not DEPLOYMENT_PATTERN.match(model["deployment"]):
            raise ValueError(f"{where}: deployment may only contain letters, digits, '.', '_' and '-'")
        endpoint = model["endpoints"].get(environment)
        if not endpoint:
            raise ValueError(f"{where}: no endpoint for environment '{environment}'")
        if not ENDPOINT_PATTERN.match(normalize_endpoint(endpoint)):
            raise ValueError(
                f"{where}: endpoint for '{environment}' must be https://<resource>.openai.azure.com, "
                ".cognitiveservices.azure.com or .services.ai.azure.com with no path"
            )
        if allowed_endpoints is not None and normalize_endpoint(endpoint) not in allowed_endpoints:
            raise ValueError(f"{where}: endpoint {endpoint} is not in the allowed model endpoints for '{environment}'")
        seen.add(model["name"])


def run(config: dict, environment: str, artifacts: Path) -> None:
    models = config.get("models") or []
    for folder in ("apis", "backends"):
        shutil.rmtree(artifacts / folder, ignore_errors=True)

    for model in models:
        name = model["name"]
        write_json(
            artifacts / "backends" / backend_id(name) / "backendInformation.json",
            {
                "properties": {
                    "description": f"{model['displayName']} model endpoint",
                    "url": normalize_endpoint(model["endpoints"][environment]) + "/openai",
                    "protocol": "http",
                }
            },
        )

        api_dir = artifacts / "apis" / api_id(name)
        write_json(
            api_dir / "apiInformation.json",
            {
                "properties": {
                    "displayName": model["displayName"],
                    "description": model.get("description", ""),
                    "path": f"llm/{name}/openai",
                    "protocols": ["https"],
                    "subscriptionRequired": True,
                    # Lets Azure OpenAI SDKs pass the APIM subscription key as their api-key.
                    "subscriptionKeyParameterNames": {"header": "api-key", "query": "subscription-key"},
                }
            },
        )
        write_json(api_dir / "specification.json", {**OPENAPI_SPEC, "info": {**OPENAPI_SPEC["info"], "title": model["displayName"]}})
        write_text(
            api_dir / "policy.xml",
            API_POLICY.replace("__BACKEND_ID__", backend_id(name)).replace("__DEPLOYMENT__", model["deployment"]),
        )

    log.info("Onboarded %d model(s) for %s: %s", len(models), environment, ", ".join(api_id(m["name"]) for m in models))


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, indent=2) + "\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
