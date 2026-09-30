from pathlib import Path

import pytest

from tests._distributions import register_distribution
from tests.hosts_example.broken import (
    ExplodingHost,
    OutdatedHost,
    UnansweringHost,
    UnresolvableEventHost,
)

_EXAMPLE_ENTRY_POINTS = {
    'example': 'tests.hosts_example.host:ExampleHost',
    'exploding': f'{ExplodingHost.__module__}:{ExplodingHost.__qualname__}',
    'outdated': f'{OutdatedHost.__module__}:{OutdatedHost.__qualname__}',
    'runtime-consume-failure': 'tests.hosts_example.broken:RuntimeConsumeFailureHost',
    'runtime-finalize-failure': 'tests.hosts_example.broken:RuntimeFinalizeFailureHost',
    'runtime-should-run-failure': 'tests.hosts_example.broken:RuntimeShouldRunFailureHost',
    'runtime-step-input-failure': 'tests.hosts_example.broken:RuntimeStepInputFailureHost',
    'unanswering': f'{UnansweringHost.__module__}:{UnansweringHost.__qualname__}',
    'unconstructible': 'tests.hosts_example.broken:UnconstructibleHost',
    'unimportable': 'tests.hosts_example.unimportable:Missing',
    'unresolvable-event': (
        f'{UnresolvableEventHost.__module__}:{UnresolvableEventHost.__qualname__}'
    ),
}


@pytest.fixture
def example_hosts(distribution_root: Path) -> Path:
    register_distribution(
        distribution_root, 'sequential-hooks-host-example', _EXAMPLE_ENTRY_POINTS
    )
    return distribution_root
