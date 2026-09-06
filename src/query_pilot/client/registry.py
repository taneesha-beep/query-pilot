"""Logical name to provider, pinned model string and credential pool.

A caller asks for ``cheap`` or ``strong``. It cannot ask for a model: there is no argument
anywhere in this package that takes a model string from a call site, which is the point.
The registry turns a role into an ordered list of candidates — the primary endpoint on
each of its pools, then each spillover endpoint on each of its pools — and quota control
walks that list. In 1.1 the first candidate is taken; in 1.2 the walk is governed by
whether a candidate's buckets can admit the call.

Candidates are ordered rather than balanced. Draining pool 1 before touching pool 2 is
what keeps a second pool genuinely in reserve, and for a per-minute ceiling it is also the
behaviour that leaves the most headroom where it is most likely to be needed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from query_pilot.client.config import ClientConfig, Endpoint, discover_pools
from query_pilot.client.errors import ConfigError
from query_pilot.client.http import HttpClient
from query_pilot.client.providers.base import Provider, build_provider
from query_pilot.client.types import Credential


@dataclass(frozen=True, slots=True)
class Candidate:
    """One place a role's call can go: a pinned model on a provider on one quota pool.

    ``key`` is what quota control keys a bucket on — **provider, pool and model**, not
    provider alone. Groq serves requests-per-day and tokens-per-minute per model per
    organization; Google serves requests-per-minute per model per project, and a pool is a
    project. A bucket keyed on the provider would be wrong in both directions at once: it
    would block a model that has quota left because a sibling model spent its own, and let
    through a call that is certain to be refused.
    """

    role: str
    endpoint: Endpoint
    provider: Provider
    credential: Credential

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.endpoint.provider, self.credential.pool, self.endpoint.model)

    def __str__(self) -> str:
        return f"{self.endpoint.name}[{self.credential.pool}]"


class Registry:
    """Builds one adapter per provider per pool, and resolves roles to candidates."""

    def __init__(
        self,
        config: ClientConfig,
        http: HttpClient,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._http = http
        self._environ = dict(os.environ if environ is None else environ)
        self._pools: dict[str, tuple[Credential, ...]] = {}
        self._adapters: dict[tuple[str, str], Provider] = {}

    def pools(self, provider: str) -> tuple[Credential, ...]:
        """Every quota pool configured for a provider, in order. Empty if no key is set."""
        if provider not in self._pools:
            spec = self.config.providers.get(provider)
            if spec is None:
                raise ConfigError(f"unknown provider {provider!r}")
            self._pools[provider] = discover_pools(provider, spec.key_env, self._environ)
        return self._pools[provider]

    def adapter(self, provider: str, credential: Credential) -> Provider:
        cached = self._adapters.get((provider, credential.pool))
        if cached is None:
            cached = build_provider(self.config.providers[provider], credential, self._http)
            self._adapters[(provider, credential.pool)] = cached
        return cached

    def candidates(self, role: str) -> tuple[Candidate, ...]:
        """The ordered places this role's work may go.

        Raises rather than returning empty when nothing is reachable: a role with no
        candidates means a key is missing, and reporting that as "all pools busy" later
        would send someone looking for a quota problem that is really an unset variable.
        """
        spec = self.config.roles.get(role)
        if spec is None:
            known = ", ".join(sorted(self.config.roles)) or "none configured"
            raise ConfigError(f"unknown role {role!r}; configured roles: {known}")

        found: list[Candidate] = []
        for endpoint_name in (spec.endpoint, *spec.spillover):
            endpoint = self.config.endpoints[endpoint_name]
            for credential in self.pools(endpoint.provider):
                found.append(
                    Candidate(
                        role=role,
                        endpoint=endpoint,
                        provider=self.adapter(endpoint.provider, credential),
                        credential=credential,
                    )
                )
        if not found:
            wanted = sorted(
                {
                    self.config.providers[self.config.endpoints[name].provider].key_env
                    for name in (spec.endpoint, *spec.spillover)
                }
            )
            raise ConfigError(
                f"role {role!r} has no credential pool; set one of: {', '.join(wanted)}"
            )
        return tuple(found)

    def endpoints_for(self, role: str) -> Sequence[Endpoint]:
        spec = self.config.roles[role]
        return [self.config.endpoints[name] for name in (spec.endpoint, *spec.spillover)]
