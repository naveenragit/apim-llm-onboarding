"""Example consumer: reads its APIM subscription key from Key Vault and calls the model through APIM.

The key is fetched with the app's managed identity on every request, so a rotated key is picked up
without a redeploy. The response never contains the key.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

import azure.functions as func
from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

API_VERSION = "2024-10-21"
MAX_PROMPT_CHARS = 500

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
_secrets = SecretClient(
    os.environ["KEY_VAULT_URL"], ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
)


def _respond(status: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, indent=2), status_code=status, mimetype="application/json")


@app.route(route="chat", methods=["GET", "POST"])
def chat(req: func.HttpRequest) -> func.HttpResponse:
    prompt = (req.params.get("prompt") or "Reply with OK.")[:MAX_PROMPT_CHARS]
    vault_host = urllib.parse.urlparse(os.environ["KEY_VAULT_URL"]).hostname
    diagnostics = {"vaultResolvesTo": socket.gethostbyname(vault_host)}

    try:
        secret = _secrets.get_secret(os.environ["SECRET_NAME"])
    except AzureError as error:
        return _respond(502, {**diagnostics, "error": f"Key Vault: {type(error).__name__}"})
    diagnostics.update(
        secretVersion=secret.properties.version,
        secretExpires=secret.properties.expires_on.isoformat() if secret.properties.expires_on else None,
        keyFingerprint=hashlib.sha256(secret.value.encode()).hexdigest()[:16],
    )

    url = (
        f"{os.environ['APIM_GATEWAY_URL'].rstrip('/')}/llm/{os.environ['MODEL_NAME']}/openai/deployments/"
        f"{os.environ['DEPLOYMENT_NAME']}/chat/completions?api-version={API_VERSION}"
    )
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_completion_tokens": 512}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"api-key": secret.value, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
            return _respond(200, {
                **diagnostics,
                "status": response.status,
                "reply": result["choices"][0]["message"]["content"],
                "remainingTokensThisMinute": response.headers.get("x-ratelimit-remaining-tokens"),
            })
    except urllib.error.HTTPError as error:
        return _respond(502, {**diagnostics, "status": error.code, "error": error.read().decode(errors="replace")[:300]})
    except urllib.error.URLError as error:
        return _respond(502, {**diagnostics, "error": str(error.reason)})
