"""Activity 4: grant developers access to published products.

For each grant under `access.<environment>` in config.yaml:
  * ensures the developer has an active APIM user account
  * ensures the developer has one product subscription in the requested state, whatever its ID
  * optionally keeps the active subscription's primary key current in Azure Key Vault, rotating it
    before the secret expires
Then reports product subscriptions that no grant manages.

Keys are never printed or written to Git. Removing a grant does not revoke access;
set its state to suspended or cancelled instead.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.keyvault.secrets import SecretClient
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import (
    SubscriptionCreateParameters,
    SubscriptionUpdateParameters,
    UserCreateParameters,
    UserIdentityContract,
)

from create_product import product_id, release_check_id

log = logging.getLogger("grant_access")

VALID_STATES = ("active", "suspended", "cancelled")
INACTIVE_SUBSCRIPTION_STATES = {"cancelled", "rejected", "expired"}
EMAIL_PATTERN = re.compile(r"^[^@\s']+@[^@\s']+\.[^@\s']+$")
KEY_LIFETIME = timedelta(days=90)
ROTATE_BEFORE_EXPIRY = timedelta(days=14)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def arm_tail(resource_id: str | None) -> str:
    """Returns '<type>/<name>' from an ARM ID, e.g. 'products/llm-gpt-4o-standard'."""
    return "/".join((resource_id or "").rstrip("/").split("/")[-2:]).lower()


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
        # The hash keeps IDs unique when emails differ only in punctuation, e.g. a.b@ and a-b@.
        return f"{slugify(self.email)[:60]}-{hashlib.sha256(self.email.encode()).hexdigest()[:6]}"

    @property
    def subscription_id(self) -> str:
        return f"{self.product}--{self.user_id}"

    @property
    def display_name(self) -> str:
        return f"{self.product} - {self.email}"[:100]

    @property
    def key_secret_name(self) -> str:
        return f"apim-{self.subscription_id}"


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
        self._managed: set[tuple[str, str]] = set()

    def apply(self, grant: Grant) -> None:
        product_arm_id = self._product_arm_id(grant.product)
        if product_arm_id is None:
            log.info("[dry-run] would grant %s access to %s once the product is published", grant.email, grant.product)
            return
        owner_arm_id = self._ensure_user(grant)
        subscription = self._ensure_subscription(grant, owner_arm_id, product_arm_id)
        if subscription:
            self._managed.add((grant.product, subscription))
        if subscription and self._secrets and grant.state == "active":
            self._deliver_key(grant, subscription)

    def report_unmanaged(self, product: str) -> int:
        """Warns about live product subscriptions that no grant manages, e.g. approved in the portal."""
        try:
            subscriptions = list(self._client.product_subscriptions.list(*self._scope, product))
        except ResourceNotFoundError:
            return 0
        unmanaged = [
            subscription
            for subscription in subscriptions
            if subscription.name != release_check_id(product)
            and (product, subscription.name) not in self._managed
            and subscription.state not in INACTIVE_SUBSCRIPTION_STATES
        ]
        for subscription in unmanaged:
            log.warning(
                "%s: subscription %s (%s, owner %s) is not managed by config.yaml",
                product, subscription.name, subscription.state, arm_tail(subscription.owner_id) or "none",
            )
        return len(unmanaged)

    def _product_arm_id(self, product: str) -> str | None:
        if product not in self._product_ids:
            try:
                self._product_ids[product] = self._client.product.get(*self._scope, product).id
            except ResourceNotFoundError:
                # A dry run previews publish without creating the product, so it may not exist yet.
                if self._dry_run:
                    return None
                raise ValueError(f"product '{product}' does not exist; publish it first") from None
        return self._product_ids[product]

    def _find_user(self, grant: Grant):
        # APIM allows one user per email, so email is the authoritative match (covers portal sign-ups).
        email_filter = "email eq '{}'".format(grant.email.replace("'", "''"))
        for user in self._client.user.list_by_service(*self._scope, filter=email_filter):
            if (user.email or "").lower() == grant.email:
                if grant.state == "active":
                    self._check_user(grant, user)
                return user
        try:
            user = self._client.user.get(*self._scope, grant.user_id)
        except ResourceNotFoundError:
            return None
        raise ValueError(f"user id '{grant.user_id}' already belongs to {user.email}")

    def _check_user(self, grant: Grant, user) -> None:
        # Portal sign-ups can be unconfirmed or blocked; never hand an active key to those accounts.
        if user.state != "active":
            raise ValueError(f"APIM user {user.name} is {user.state}; resolve it in APIM before granting access")
        if grant.entra_object_id:
            identities = self._client.user.get(*self._scope, user.name).identities or []
            if not any(i.provider == "aad" and i.id == grant.entra_object_id for i in identities):
                raise ValueError(f"APIM user {user.name} is not linked to Entra object {grant.entra_object_id}")

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

    def _find_subscription(self, product: str, owner_arm_id: str | None):
        # Matches by owner rather than ID, so portal-requested subscriptions (random IDs) are found too.
        if owner_arm_id is None:
            return None
        matches = [
            subscription
            for subscription in self._client.product_subscriptions.list(*self._scope, product)
            if arm_tail(subscription.owner_id) == arm_tail(owner_arm_id)
        ]
        if len(matches) > 1:
            names = ", ".join(subscription.name for subscription in matches)
            raise ValueError(f"user has {len(matches)} subscriptions to {product} ({names}); remove the extras first")
        return matches[0] if matches else None

    def _ensure_subscription(self, grant: Grant, owner_arm_id: str | None, product_arm_id: str) -> str | None:
        """Returns the name of the developer's subscription, or None if a dry run would create it."""
        existing = self._find_subscription(grant.product, owner_arm_id)

        if existing is None:
            try:
                taken = self._client.subscription.get(*self._scope, grant.subscription_id)
            except ResourceNotFoundError:
                taken = None
            if taken is not None:
                raise ValueError(
                    f"subscription id '{grant.subscription_id}' is already used by owner '{taken.owner_id}' "
                    f"on '{taken.scope}'"
                )
            if self._dry_run:
                log.info("[dry-run] would create %s subscription %s", grant.state, grant.subscription_id)
                return None
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
            return grant.subscription_id

        if existing.state == grant.state:
            log.info("Subscription %s already %s", existing.name, grant.state)
            return existing.name

        if self._dry_run:
            log.info("[dry-run] would change %s from %s to %s", existing.name, existing.state, grant.state)
            return existing.name
        self._client.subscription.update(
            *self._scope,
            existing.name,
            if_match="*",
            parameters=SubscriptionUpdateParameters(state=grant.state),
            notify=self._notify,
        )
        log.info("Changed %s from %s to %s", existing.name, existing.state, grant.state)
        return existing.name

    def _deliver_key(self, grant: Grant, subscription: str) -> None:
        # Runs every time, so a failed delivery or rotation is retried by an unchanged rerun.
        key = self._client.subscription.list_secrets(*self._scope, subscription).primary_key
        delivered = self._latest_secret_version(grant.key_secret_name)
        now = datetime.now(timezone.utc)
        current = delivered is not None and (delivered.tags or {}).get("keyFingerprint") == key_fingerprint(key)
        expiring = delivered is not None and delivered.expires_on is not None and delivered.expires_on - now < ROTATE_BEFORE_EXPIRY
        if current and not expiring:
            return
        # Rotate only when the vault holds the live key; otherwise a rotated key is still awaiting delivery.
        rotate = current and expiring
        if self._dry_run:
            action = "rotate and store the key" if rotate else "store the key"
            log.info("[dry-run] would %s for %s in Key Vault secret %s", action, subscription, grant.key_secret_name)
            return
        if rotate:
            self._client.subscription.regenerate_primary_key(*self._scope, subscription)
            key = self._client.subscription.list_secrets(*self._scope, subscription).primary_key
            log.info("Rotated the primary key of %s; the previous primary key no longer works", subscription)
        self._secrets.set_secret(
            grant.key_secret_name,
            key,
            content_type="APIM subscription key",
            expires_on=now + KEY_LIFETIME,
            tags={"apimSubscription": subscription, "product": grant.product, "keyFingerprint": key_fingerprint(key)},
        )
        log.info("Stored key for %s in Key Vault secret %s", subscription, grant.key_secret_name)

    def _latest_secret_version(self, name: str):
        # Reads properties only: an expired secret's value can't be read, but its metadata can.
        try:
            versions = list(self._secrets.list_properties_of_secret_versions(name))
        except ResourceNotFoundError:
            return None
        return max(versions, key=lambda version: version.created_on, default=None)


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

    unmanaged = 0
    for product in config.get("products") or []:
        try:
            unmanaged += provisioner.report_unmanaged(product_id(product["name"]))
        except HttpResponseError as error:
            log.warning("could not check %s for unmanaged subscriptions: %s", product_id(product["name"]), error.message)
    log.info(
        "Processed %d grant(s) for %s, %d failed, %d unmanaged subscription(s)", len(grants), environment, failures, unmanaged
    )
    return failures
