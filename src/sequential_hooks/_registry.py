"""Discover and load host adapters through entry points."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast

from sequential_hooks.hosts import API_VERSION, HostSpec

if TYPE_CHECKING:
    from importlib.metadata import EntryPoints

_ENTRY_POINT_GROUP = 'sequential_hooks.hosts'
_REQUIRED_ATTRIBUTES = (
    'adapter',
    'configuration_response',
    'doctor_loader',
    'events',
    'name',
    'payload_event',
    'requires_event',
)


class HostLoadError(Exception):
    """Report that no functioning host adapter exists for the selected name."""


def _entry_points() -> EntryPoints:
    """Read host entry points without loading metadata during CLI import."""
    # Delay importing metadata until the caller selects or inventories hosts.
    module = import_module('importlib.metadata')
    return cast('EntryPoints', module.entry_points(group=_ENTRY_POINT_GROUP))


def _registered_hosts() -> list[str]:
    """Return sorted names in the host entry-point group."""
    return sorted({entry.name for entry in _entry_points()})


def _validate_host(name: str, spec: object) -> HostSpec:
    """Validate and return one constructed host specification."""
    for attribute in _REQUIRED_ATTRIBUTES:
        if not hasattr(spec, attribute):
            raise HostLoadError(f'host {name!r} does not provide {attribute}')
    version = getattr(spec, 'api_version', None)
    if version != API_VERSION:
        raise HostLoadError(
            f'host {name!r} implements host API {version!r}; '
            f'sequential-hooks requires {API_VERSION}'
        )
    host_spec = cast('HostSpec', spec)
    reported_name = host_spec.name
    if reported_name != name:
        raise HostLoadError(f'host {name!r} reports the name {reported_name!r}')
    return host_spec


def load_host(name: str) -> HostSpec:
    """Load the host registered under one entry-point name.

    Args:
        name: Host name given to `--agent`.

    Returns:
        The registered host specification.

    Raises:
        HostLoadError: If the name is unregistered or registered more than
            once, the entry point cannot be loaded, the object lacks a contract
            attribute, its `api_version` differs from `API_VERSION`, or its
            reported `name` differs from the entry-point name.
    """
    matches = [entry for entry in _entry_points() if entry.name == name]
    if not matches:
        hosts = ', '.join(_registered_hosts()) or 'none'
        raise HostLoadError(f'unknown host {name!r}; registered hosts: {hosts}')
    if len(matches) > 1:
        raise HostLoadError(f'host {name!r} is registered {len(matches)} times')
    try:
        factory = matches[0].load()
        spec = _validate_host(name, factory())
    except HostLoadError:
        raise
    except Exception as error:
        raise HostLoadError(f'host {name!r} failed to load: {error}') from error
    return spec


def host_requirements() -> dict[str, HostSpec]:
    """Return each loadable host specification keyed by its registered name.

    Returns:
        Names in sorted order; hosts that fail to load are omitted.
    """
    requirements: dict[str, HostSpec] = {}
    for name in _registered_hosts():
        try:
            spec = load_host(name)
        except HostLoadError:
            continue
        requirements[name] = spec
    return requirements
