"""Activity 4: grant developers access to published products.

For each grant under `access.<environment>` in config.yaml:
  * ensures the developer has an APIM user account
  * ensures a product-scoped subscription exists in the requested state
  * optionally stores the APIM-generated primary key in Azure Key Vault

Keys are never printed or written to Git. Removing a grant does not revoke access;
set its state to suspended or cancelled instead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.keyvault.secrets import SecretClient
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import (
    SubscriptionCreateParameters,
    SubscriptionUpdateParameters,
    UserCreateParameters,
    UserIdentityContract,
)

from create_product import product_id

log = logging.getLogger("grant_access")

VALID_STATES = ("active", "suspended", "cancelled")
EMAIL_PATTERN = re.compile(r"^[^@\s']+@[^@\s']+\.[^@\s']+$")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass(frozen=True)
class Grant:
    email: str
    first_name: str
    last_name: str
    product: str
    state: str = "active"
    entra_object_id: str | None = None

    @property
    def user_id(self) -> str:
        return slugify(self.email)[:80]

    @property
    def subscription_id(self) -> str:
        return f"{self.product}--{slugify(self.email)}"[:256]

    @property
    def display_name(self) -> str:
        return f"{self.product} - {self.email}"[:100]

    @property
    def key_secret_name(self) -> str:
        return f"apim-{self.subscription_id}"[:127]


def load_grants(config: dict, environment: str) -> list[Grant]:
    product_names = {product["name"] for product in config.get("products") or []}
    entries = (config.get("access") or {}).get(environment) or []
    grants, seen = [], set()
    for index, entry in enumerate(entries, start=1):
        where = f"access.{environment} #{index}"
        missing = [field for field in ("email", "firstName", "lastName", "product") if not entry.get(field)]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if not EMAIL_PATTERN.match(entry["email"]):
            raise ValueError(f"{where}: invalid email '{entry['email']}'")
        if entry["product"] not in product_names:
            raise ValueError(f"{where}: unknown product '{entry['product']}'")
        state = entry.get("state", "active")
        if state not in VALID_STATES:
            raise ValueError(f"{where}: state must be one of {', '.join(VALID_STATES)}")

        grant = Grant(
            email=entry["email"].lower(),
            first_name=entry["firstName"],
            last_name=entry["lastName"],
            product=product_id(entry["product"]),
            state=state,
            entra_object_id=entry.get("entraObjectId"),
        )
        if grant.subscription_id in seen:
            raise ValueError(f"{where}: duplicate grant for {grant.email} on {entry['product']}")
        seen.add(grant.subscription_id)
        grants.append(grant)
    return grants


class AccessProvisioner:
    def __init__(
        self,
        client: ApiManagementClient,
        resource_group: str,
        service_name: str,
        *,
        secrets: SecretClient | None = None,
        notify: bool = False,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._scope = (resource_group, service_name)
        self._secrets = secrets
        self._notify = notify
        self._dry_run = dry_run
        self._product_ids: dict[str, str] = {}

    def apply(self, grant: Grant) -> None:
        product_arm_id = self._product_arm_id(grant.product)
        owner_arm_id = self._ensure_user(grant)
        activated = self._ensure_subscription(grant, owner_arm_id, product_arm_id)
        if activated and self._secrets:
            self._store_key(grant)

    def _product_arm_id(self, product: str) -> str:
        if product not in self._product_ids:
            try:
                self._product_ids[product] = self._client.product.get(*self._scope, product).id
            except ResourceNotFoundError:
                raise ValueError(f"product '{product}' does not exist; publish it first") from None
        return self._product_ids[product]

    def _find_user(self, grant: Grant):
        try:
            return self._client.user.get(*self._scope, grant.user_id)
        except ResourceNotFoundError:
            pass
        # Users who self-registered in the developer portal have generated ids.
        email_filter = "email eq '{}'".format(grant.email.replace("'", "''"))
        return next(iter(self._client.user.list_by_service(*self._scope, filter=email_filter)), None)

    def _ensure_user(self, grant: Grant) -> str | None:
        user = self._find_user(grant)
        if user:
            return user.id
        if self._dry_run:
            log.info("[dry-run] would create user %s", grant.email)
            return None

        identities = (
            [UserIdentityContract(provider="aad", id=grant.entra_object_id)] if grant.entra_object_id else None
        )
        user = self._client.user.create_or_update(
            *self._scope,
            grant.user_id,
            UserCreateParameters(
                email=grant.email,
                first_name=grant.first_name,
                last_name=grant.last_name,
                identities=identities,
                note="Provisioned by the developer access pipeline.",
            ),
            notify=self._notify,
        )
        log.info("Created user %s", grant.email)
        return user.id

    def _ensure_subscription(self, grant: Grant, owner_arm_id: str | None, product_arm_id: str) -> bool:
        """Returns True when the subscription was newly activated."""
        try:
            existing = self._client.subscription.get(*self._scope, grant.subscription_id)
        except ResourceNotFoundError:
            existing = None

        if existing is None:
            if self._dry_run:
                log.info("[dry-run] would create %s subscription %s", grant.state, grant.subscription_id)
                return False
            self._client.subscription.create_or_update(
                *self._scope,
                grant.subscription_id,
                SubscriptionCreateParameters(
                    owner_id=owner_arm_id,
                    scope=product_arm_id,
                    display_name=grant.display_name,
                    state=grant.state,
                ),
                notify=self._notify,
            )
            log.info("Created %s subscription %s", grant.state, grant.subscription_id)
            return grant.state == "active"

        if existing.state == grant.state:
            log.info("Subscription %s already %s", grant.subscription_id, grant.state)
            return False

        if self._dry_run:
            log.info("[dry-run] would change %s from %s to %s", grant.subscription_id, existing.state, grant.state)
            return False
        self._client.subscription.update(
            *self._scope,
            grant.subscription_id,
            if_match="*",
            parameters=SubscriptionUpdateParameters(state=grant.state),
            notify=self._notify,
        )
        log.info("Changed %s from %s to %s", grant.subscription_id, existing.state, grant.state)
        return grant.state == "active"

    def _store_key(self, grant: Grant) -> None:
        keys = self._client.subscription.list_secrets(*self._scope, grant.subscription_id)
        self._secrets.set_secret(
            grant.key_secret_name,
            keys.primary_key,
            content_type="APIM subscription key",
            tags={"apimSubscription": grant.subscription_id, "product": grant.product},
        )
        log.info("Stored key for %s in Key Vault secret %s", grant.subscription_id, grant.key_secret_name)


def run(
    config: dict,
    environment: str,
    client: ApiManagementClient,
    resource_group: str,
    service_name: str,
    *,
    secrets: SecretClient | None = None,
    notify: bool = False,
    dry_run: bool = False,
) -> int:
    """Applies every grant and returns the number that failed."""
    grants = load_grants(config, environment)
    provisioner = AccessProvisioner(
        client, resource_group, service_name, secrets=secrets, notify=notify, dry_run=dry_run
    )
    failures = 0
    for grant in grants:
        try:
            provisioner.apply(grant)
        except (ValueError, HttpResponseError) as error:
            failures += 1
            log.error("%s on %s: %s", grant.email, grant.product, error)
    log.info("Processed %d grant(s) for %s, %d failed", len(grants), environment, failures)
    return failures
