import sys
from types import ModuleType

import pytest

import sequential_hooks._containment as containment_module
from sequential_hooks._containment import (
    ContainmentPlatform,
    _windows_api,
)


@pytest.mark.parametrize(
    ('containment', 'expected_module', 'factory_name'),
    [
        (
            ContainmentPlatform.POSIX,
            'sequential_hooks._containment._posix',
            'PosixBackend',
        ),
        (
            ContainmentPlatform.WINDOWS,
            'sequential_hooks._containment._windows',
            'WindowsBackend',
        ),
        (
            ContainmentPlatform.UNAVAILABLE,
            'sequential_hooks._containment._uncontained',
            'UncontainedBackend',
        ),
    ],
)
def test_current_backend_imports_only_the_classified_backend(
    monkeypatch: pytest.MonkeyPatch,
    containment: ContainmentPlatform,
    expected_module: str,
    factory_name: str,
) -> None:
    selected_imports: list[str] = []
    resolutions: list[tuple[str | None, str | None]] = []
    expected_instance = object()

    def selected_factory() -> object:
        selected_imports.append(expected_module)
        return expected_instance

    def record_resolution(
        *,
        implementation: str | None = None,
        platform_name: str | None = None,
    ) -> ContainmentPlatform:
        resolutions.append((implementation, platform_name))
        return containment

    selected_module = ModuleType(expected_module)
    setattr(selected_module, factory_name, selected_factory)
    monkeypatch.setitem(sys.modules, expected_module, selected_module)
    monkeypatch.setattr(containment_module, '_resolve_runtime', record_resolution)

    backend = containment_module.current_backend(
        implementation='IgnoredPython',
        platform_name='ignored-platform',
    )

    assert selected_imports == [expected_module]
    assert backend is expected_instance
    assert resolutions == [('IgnoredPython', 'ignored-platform')]


def test_current_backend_returns_a_fresh_instance() -> None:
    first = containment_module.current_backend(
        implementation='CPython',
        platform_name='linux',
    )
    second = containment_module.current_backend(
        implementation='CPython',
        platform_name='linux',
    )

    assert first is not second


def test_non_native_windows_reports_unavailable_without_api_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _windows_api,
        'NativeWindowsApi',
        lambda: pytest.fail('unavailable platform probed Windows'),
    )

    containment, configurable, limitations = containment_module.probe_current_containment(
        implementation='PyPy',
        platform_name='win32',
    )

    assert containment.value == 'uncontained'
    assert configurable is None
    assert limitations


@pytest.mark.parametrize(
    ('implementation', 'platform_name', 'expected'),
    [
        ('CPython', 'darwin', ContainmentPlatform.POSIX),
        ('CPython', 'linux', ContainmentPlatform.POSIX),
        ('CPython', 'win32', ContainmentPlatform.WINDOWS),
        ('CPython', 'cygwin', ContainmentPlatform.UNAVAILABLE),
        ('CPython', 'msys', ContainmentPlatform.UNAVAILABLE),
        ('CPython', 'freebsd14', ContainmentPlatform.UNAVAILABLE),
        ('PyPy', 'darwin', ContainmentPlatform.UNAVAILABLE),
        ('PyPy', 'linux', ContainmentPlatform.UNAVAILABLE),
        ('PyPy', 'win32', ContainmentPlatform.UNAVAILABLE),
    ],
)
def test_static_probe_classifies_supported_runtime_exactly(
    monkeypatch: pytest.MonkeyPatch,
    implementation: str,
    platform_name: str,
    expected: ContainmentPlatform,
) -> None:
    monkeypatch.setattr(_windows_api, 'probe_job_object', lambda: True)

    containment, _, _ = containment_module.probe_current_containment(
        implementation=implementation,
        platform_name=platform_name,
    )

    assert containment is expected


def test_static_probe_consumes_resolved_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    def record_resolution(
        *,
        implementation: str | None = None,
        platform_name: str | None = None,
    ) -> ContainmentPlatform:
        calls.append((implementation, platform_name))
        return ContainmentPlatform.POSIX

    monkeypatch.setattr(containment_module, '_resolve_runtime', record_resolution)

    result = containment_module.probe_current_containment(
        implementation='IgnoredPython',
        platform_name='ignored-platform',
    )

    assert result == (ContainmentPlatform.POSIX, None, ())
    assert calls == [('IgnoredPython', 'ignored-platform')]


def test_windows_platform_probe_never_assigns_a_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class FakeWindowsApi:
        def assign_pid(self, *_args: object) -> None:
            pytest.fail('containment probe assigned a process')

        def close_handle(self, handle: int) -> None:
            events.append(('close', handle))

        def create_kill_on_close_job(self) -> int:
            events.append('create')
            return 42

    monkeypatch.setattr(_windows_api, 'NativeWindowsApi', FakeWindowsApi)

    containment, configurable, limitations = containment_module.probe_current_containment(
        implementation='CPython',
        platform_name='win32',
    )

    assert containment.value == 'windows'
    assert configurable is True
    assert limitations == ()
    assert events == ['create', ('close', 42)]
