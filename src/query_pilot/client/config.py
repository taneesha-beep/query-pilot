"""Read `config/providers.toml`, and find the credential pools the environment holds.

**Every model string this package sends lives in that file**, which is the whole mitigation
for a provider retiring a model underneath a running project. It is not a hypothetical:
``gemini-2.5-flash`` was listed by Google's models endpoint and returned 404 as retired on
2026-09-06, before any of this code existed. When that happens again the fix is one line in
a configuration file rather than a search through call sites.

A limit that is absent from the file is **unmodelled, not unlimited**. Google publishes no
per-model free-tier figures and serves no rate-limit headers, so its daily allowance for
the model this project spills onto is genuinely unknown; writing a plausible number here
would turn a known gap into an invented measurement.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from query_pilot.client.errors import ConfigError
from query_pilot.client.providers.base import ProviderSpec
from query_pilot.client.types import Credential

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "providers.toml"

# How far past `_2` to look for pools. High enough that a real one is never missed, low
# enough that a typo is reported rather than silently skipped over.
MAX_POOLS = 9


@dataclass(frozen=True, slots=True)
class Limits:
    """Quota ceilings for one model on one pool. ``None`` means unmodelled.

    ``sources`` records how each figure was come by — ``observed`` where a request was
    refused and the provider named the ceiling, ``declared`` where the provider stated it
    in a header or its documentation and this project has never reached it. The two are not
    the same grade of evidence and the distinction is kept rather than flattened.
    """

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A provider and a pinned model string, under a name a role can point at."""

    name: str
    provider: str
    model: str
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """A logical name — ``cheap``, ``strong`` — and where its work goes.

    ``endpoint`` is the one value that moves a role onto another provider. ``spillover``
    is tried in order, and only once every pool of the primary is refusing.
    """

    name: str
    endpoint: str
    spillover: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClientSettings:
    max_concurrency: int = 8
    per_provider_concurrency: int = 4
    wait_ceiling_s: float = 60.0


@dataclass(frozen=True, slots=True)
class ClientConfig:
    settings: ClientSettings
    providers: Mapping[str, ProviderSpec]
    endpoints: Mapping[str, Endpoint]
    roles: Mapping[str, RoleSpec]

    @classmethod
    def load(cls, path: Path | str | None = None) -> ClientConfig:
        source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        try:
            raw = tomllib.loads(source.read_text())
        except FileNotFoundError as exc:
            raise ConfigError(f"no provider configuration at {source}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{source}: {exc}") from exc
        return cls.from_mapping(raw, source=str(source))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, source: str = "<mapping>") -> ClientConfig:
        providers = {
            name: ProviderSpec(
                name=name,
                style=_require(entry, "style", f"{source}: provider {name!r}"),
                url=_require(entry, "url", f"{source}: provider {name!r}"),
                key_env=_require(entry, "key_env", f"{source}: provider {name!r}"),
                daily_reset_time=entry.get("daily_reset_time"),
                daily_reset_timezone=entry.get("daily_reset_timezone"),
            )
            for name, entry in (raw.get("providers") or {}).items()
        }
        endpoints: dict[str, Endpoint] = {}
        for name, entry in (raw.get("endpoints") or {}).items():
            where = f"{source}: endpoint {name!r}"
            provider = _require(entry, "provider", where)
            if provider not in providers:
                raise ConfigError(f"{where}: unknown provider {provider!r}")
            endpoints[name] = Endpoint(
                name=name,
                provider=provider,
                model=_require(entry, "model", where),
                limits=Limits(
                    rpm=entry.get("rpm"),
                    rpd=entry.get("rpd"),
                    tpm=entry.get("tpm"),
                    tpd=entry.get("tpd"),
                    sources=dict(entry.get("sources") or {}),
                ),
            )
        roles: dict[str, RoleSpec] = {}
        for name, entry in (raw.get("roles") or {}).items():
            where = f"{source}: role {name!r}"
            spillover = tuple(entry.get("spillover") or ())
            for referenced in (_require(entry, "endpoint", where), *spillover):
                if referenced not in endpoints:
                    raise ConfigError(f"{where}: unknown endpoint {referenced!r}")
            roles[name] = RoleSpec(name=name, endpoint=entry["endpoint"], spillover=spillover)

        settings_raw = raw.get("client") or {}
        settings = ClientSettings(
            max_concurrency=int(settings_raw.get("max_concurrency", 8)),
            per_provider_concurrency=int(settings_raw.get("per_provider_concurrency", 4)),
            wait_ceiling_s=float(settings_raw.get("wait_ceiling_s", 60.0)),
        )
        if not providers or not endpoints or not roles:
            raise ConfigError(f"{source}: needs at least one provider, endpoint and role")
        return cls(settings=settings, providers=providers, endpoints=endpoints, roles=roles)


def _require(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: missing {key!r}")
    return value


def discover_pools(
    provider: str, key_env: str, environ: Mapping[str, str] | None = None
) -> tuple[Credential, ...]:
    """Find every quota pool a provider has keys for, in order.

    ``KEY``, then ``KEY_2``, ``KEY_3``, and so on, contiguously. **Each suffix is its own
    quota pool, never a fallback key for the same one** — the convention this project fixed
    at 0.4 after establishing that Google enforces per Cloud project rather than per key,
    so two keys from two projects are two ceilings and two keys from one project are one.

    A gap is an error rather than a stopping point. ``KEY_3`` set with ``KEY_2`` unset is
    almost always a typo, and skipping past it quietly would hide half the project's Google
    capacity behind a misspelled variable name.
    """
    env = os.environ if environ is None else environ

    def value_of(name: str) -> str | None:
        raw = env.get(name)
        return raw.strip() if raw and raw.strip() else None

    suffixed = {index: value_of(f"{key_env}_{index}") for index in range(2, MAX_POOLS + 1)}
    base = value_of(key_env)
    if base is None:
        present = [index for index, value in suffixed.items() if value]
        if present:
            raise ConfigError(
                f"{key_env}_{present[0]} is set but {key_env} is not; "
                f"suffixed keys number pools from 2 upwards"
            )
        return ()

    pools = [Credential(pool=f"{provider}#1", env_var=key_env, value=base)]
    for index in range(2, MAX_POOLS + 1):
        value = suffixed[index]
        if value is None:
            later = [n for n, v in suffixed.items() if v and n > index]
            if later:
                raise ConfigError(
                    f"{key_env}_{later[0]} is set but {key_env}_{index} is not; "
                    f"pools must be numbered without gaps"
                )
            break
        pools.append(
            Credential(pool=f"{provider}#{index}", env_var=f"{key_env}_{index}", value=value)
        )
    return tuple(pools)
