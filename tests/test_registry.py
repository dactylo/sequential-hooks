from pathlib import Path

import pytest

from sequential_hooks._registry import HostLoadError, host_requirements, load_host
from sequential_hooks.hosts import API_VERSION
from tests._distributions import BUILTIN_ENTRY_POINTS, register_distribution


class FailingMetadataHost:
    adapter = object()
    configuration_response = object()
    doctor_loader = object()
    name = 'metadata-failure'
    payload_event = object()
    requires_event = True

    @property
    def events(self) -> tuple[str, ...]:
        raise RuntimeError('host event metadata unavailable')


@pytest.mark.usefixtures('distribution_root')
def test_built_in_hosts_load_only_through_entry_points() -> None:
    requirements = host_requirements()
    assert set(requirements) == set(BUILTIN_ENTRY_POINTS)
    for name, target in BUILTIN_ENTRY_POINTS.items():
        spec = load_host(name)
        assert type(spec).__module__ == target.partition(':')[0]
        assert spec.name == name
        assert spec.api_version == API_VERSION
        assert spec.requires_event == requirements[name].requires_event
        assert spec.events == requirements[name].events


def test_registry_refuses_a_duplicated_name(example_hosts: Path) -> None:
    register_distribution(
        example_hosts,
        'sequential-hooks-host-twin',
        {'example': 'tests.hosts_example.host:ExampleHost'},
    )

    with pytest.raises(HostLoadError, match='registered 2 times'):
        load_host('example')


def test_registry_refuses_unknown_and_defective_hosts(example_hosts: Path) -> None:
    del example_hosts
    assert load_host('example').name == 'example'
    with pytest.raises(HostLoadError, match="unknown host 'missing'"):
        load_host('missing')
    with pytest.raises(HostLoadError, match='implements host API 0'):
        load_host('outdated')
    with pytest.raises(HostLoadError, match='failed to load'):
        load_host('unimportable')
    with pytest.raises(HostLoadError, match='failed to load'):
        load_host('unconstructible')


def test_registry_reports_failing_metadata_property_as_a_load_error(
    distribution_root: Path,
) -> None:
    register_distribution(
        distribution_root,
        'sequential-hooks-host-metadata-failure',
        {
            'metadata-failure': (
                f'{FailingMetadataHost.__module__}:{FailingMetadataHost.__qualname__}'
            )
        },
    )

    with pytest.raises(
        HostLoadError,
        match="host 'metadata-failure' failed to load: host event metadata unavailable",
    ):
        load_host('metadata-failure')


def test_requirements_omit_failing_metadata_and_retain_usable_hosts(
    distribution_root: Path,
) -> None:
    register_distribution(
        distribution_root,
        'sequential-hooks-host-metadata-pair',
        {
            'example': 'tests.hosts_example.host:ExampleHost',
            'metadata-failure': (
                f'{FailingMetadataHost.__module__}:{FailingMetadataHost.__qualname__}'
            ),
        },
    )

    requirements = host_requirements()

    assert 'metadata-failure' not in requirements
    assert requirements['example'].name == 'example'
