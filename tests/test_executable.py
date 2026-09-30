import subprocess
from pathlib import Path

import pytest

from sequential_hooks._executable import (
    ExecutableResolution,
    ExecutableStatus,
    resolve_executable,
)


def _make_file(path: Path, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755 if executable else 0o644)
    return path


def test_posix_resolution_accepts_absolute_executable(tmp_path: Path) -> None:
    candidate = _make_file(tmp_path / 'hook', executable=True)
    requested = candidate.as_posix()

    result = resolve_executable(
        requested,
        path_value=None,
        windows=False,
        pathext_value=None,
    )

    assert result == ExecutableResolution(requested, candidate, ExecutableStatus.RUNNABLE)


def test_posix_resolution_accepts_relative_path_with_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _make_file(tmp_path / 'bin' / 'hook', executable=True)
    monkeypatch.chdir(tmp_path)

    result = resolve_executable(
        'bin/hook',
        path_value=None,
        windows=False,
        pathext_value=None,
    )

    assert result == ExecutableResolution(
        'bin/hook',
        Path('bin/hook'),
        ExecutableStatus.RUNNABLE,
    )
    assert candidate.is_file()


def test_posix_resolution_finds_bare_name_on_injected_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _make_file(tmp_path / 'bin' / 'hook', executable=True)
    calls: list[tuple[str, str]] = []

    def find_on_posix_path(executable: str, *, path: str) -> str:
        calls.append((executable, path))
        return str(candidate)

    monkeypatch.setattr('sequential_hooks._executable.shutil.which', find_on_posix_path)

    result = resolve_executable(
        'hook',
        path_value=str(candidate.parent),
        windows=False,
        pathext_value=None,
    )

    assert result == ExecutableResolution('hook', candidate, ExecutableStatus.RUNNABLE)
    assert calls == [('hook', str(candidate.parent))]


def test_posix_resolution_rejects_directory(tmp_path: Path) -> None:
    result = resolve_executable(
        str(tmp_path),
        path_value=None,
        windows=False,
        pathext_value=None,
    )

    assert result == ExecutableResolution(str(tmp_path), None, ExecutableStatus.NOT_FOUND)


def test_posix_resolution_rejects_missing_absolute_path(tmp_path: Path) -> None:
    requested = str(tmp_path / 'missing' / 'hook')

    result = resolve_executable(
        requested,
        path_value=str(tmp_path),
        windows=False,
        pathext_value=None,
    )

    assert result.status is ExecutableStatus.NOT_FOUND
    assert result.resolved is None


def test_posix_resolution_rejects_missing_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = resolve_executable(
        'missing/hook',
        path_value=str(tmp_path),
        windows=False,
        pathext_value=None,
    )

    assert result.status is ExecutableStatus.NOT_FOUND
    assert result.resolved is None


@pytest.mark.parametrize(
    ('executable', 'path_value'),
    [
        ('', None),
        ('missing', None),
        ('missing', ''),
    ],
)
def test_posix_resolution_rejects_missing_tokens(
    executable: str,
    path_value: str | None,
) -> None:
    result = resolve_executable(
        executable,
        path_value=path_value,
        windows=False,
        pathext_value=None,
    )

    assert result == ExecutableResolution(executable, None, ExecutableStatus.NOT_FOUND)


def test_posix_resolution_requires_an_executable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _make_file(tmp_path / 'hook')
    calls: list[tuple[str, str]] = []

    def reject_non_executable(executable: str, *, path: str) -> None:
        calls.append((executable, path))

    monkeypatch.setattr('sequential_hooks._executable.shutil.which', reject_non_executable)

    result = resolve_executable(
        'hook',
        path_value=str(tmp_path),
        windows=False,
        pathext_value=None,
    )

    assert result.status is ExecutableStatus.NOT_FOUND
    assert result.resolved is None
    assert calls == [('hook', str(tmp_path))]
    assert candidate.is_file()


def test_resolution_never_starts_a_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _make_file(tmp_path / 'hook.exe', executable=True)

    def fail_if_called(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        pytest.fail('static executable resolution started a process')

    monkeypatch.setattr(subprocess, 'Popen', fail_if_called)

    result = resolve_executable(
        'hook.exe',
        path_value=str(tmp_path),
        windows=False,
        pathext_value=None,
    )

    assert result.resolved == candidate


def test_windows_resolution_accepts_direct_exe_path(tmp_path: Path) -> None:
    candidate = _make_file(tmp_path / 'tool.exe')

    result = resolve_executable(
        str(candidate),
        path_value='unused',
        windows=True,
        pathext_value='.EXE',
    )

    assert result == ExecutableResolution(str(candidate), candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize(
    ('suffix', 'direct', 'pathext_value'),
    [
        ('.com', False, None),
        ('.com', True, '.EXE'),
        ('.exe', False, None),
        ('.exe', True, '.COM'),
    ],
)
def test_windows_resolution_accepts_explicit_native_suffix_without_matching_pathext(
    tmp_path: Path,
    suffix: str,
    direct: bool,
    pathext_value: str | None,
) -> None:
    candidate = _make_file(tmp_path / f'hook{suffix}')
    requested = str(candidate) if direct else candidate.name

    result = resolve_executable(
        requested,
        path_value=str(tmp_path),
        windows=True,
        pathext_value=pathext_value,
    )

    assert result == ExecutableResolution(requested, candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize('suffix', ['.com', '.COM', '.exe', '.EXE'])
def test_windows_resolution_accepts_native_executable_suffixes(
    tmp_path: Path,
    suffix: str,
) -> None:
    candidate = _make_file(tmp_path / f'hook{suffix}')

    result = resolve_executable(
        f'hook{suffix}',
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.COM;.EXE',
    )

    assert result == ExecutableResolution(
        f'hook{suffix}',
        candidate,
        ExecutableStatus.RUNNABLE,
    )


@pytest.mark.parametrize(
    ('suffix', 'status'),
    [
        ('.EXE', ExecutableStatus.RUNNABLE),
        ('.CMD', ExecutableStatus.UNSUPPORTED_SHIM),
    ],
)
def test_windows_resolution_accepts_quoted_path_directories_in_order(
    tmp_path: Path,
    suffix: str,
    status: ExecutableStatus,
) -> None:
    first = tmp_path / 'first tools'
    second = tmp_path / 'second tools'
    candidate = _make_file(first / f'hook{suffix}')
    _make_file(second / 'hook.EXE')

    result = resolve_executable(
        'hook',
        path_value=f'"{first}";{second}',
        windows=True,
        pathext_value='.EXE;.CMD',
    )

    assert result.resolved == candidate
    assert result.status is status


def test_windows_resolution_accepts_spaces_and_non_ascii_characters(tmp_path: Path) -> None:
    directory = tmp_path / 'tools with spaces' / 'caf\u00e9'
    candidate = _make_file(directory / 'my hook.EXE')

    result = resolve_executable(
        'my hook',
        path_value=str(directory),
        windows=True,
        pathext_value='.EXE',
    )

    assert result == ExecutableResolution('my hook', candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize(
    ('suffix', 'direct', 'pathext_value'),
    [
        ('.bat', False, None),
        ('.bat', True, '.EXE'),
        ('.cmd', False, '.COM;.EXE'),
        ('.cmd', True, None),
        ('.ps1', False, '.EXE'),
        ('.ps1', True, None),
    ],
)
def test_windows_resolution_classifies_explicit_shim_without_matching_pathext(
    tmp_path: Path,
    suffix: str,
    direct: bool,
    pathext_value: str | None,
) -> None:
    candidate = _make_file(tmp_path / f'hook{suffix}')
    requested = str(candidate) if direct else candidate.name

    result = resolve_executable(
        requested,
        path_value=str(tmp_path),
        windows=True,
        pathext_value=pathext_value,
    )

    assert result == ExecutableResolution(
        requested,
        candidate,
        ExecutableStatus.UNSUPPORTED_SHIM,
        f'Use a real executable entry point for {requested}; '
        f'resolved unsupported Windows shim {candidate}.',
    )


def test_windows_resolution_expands_pathext_after_unlisted_dotted_suffix(tmp_path: Path) -> None:
    candidate = _make_file(tmp_path / 'python3.11.EXE')

    result = resolve_executable(
        'python3.11',
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.EXE',
    )

    assert result == ExecutableResolution('python3.11', candidate, ExecutableStatus.RUNNABLE)


def test_windows_resolution_matches_exe_case_insensitively(tmp_path: Path) -> None:
    candidate = _make_file(tmp_path / 'Tool.ExE')

    result = resolve_executable(
        'tool.exe',
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.COM;.EXE',
    )

    assert result == ExecutableResolution('tool.exe', candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize('pathext_value', [None, ''])
def test_windows_resolution_preserves_default_pathext_classifications(
    tmp_path: Path,
    pathext_value: str | None,
) -> None:
    runnable = _make_file(tmp_path / 'runnable.EXE')
    unsupported = _make_file(tmp_path / 'unsupported.BAT')
    (tmp_path / 'directory.EXE').mkdir()

    results = {
        requested: resolve_executable(
            requested,
            path_value=str(tmp_path),
            windows=True,
            pathext_value=pathext_value,
        )
        for requested in ('runnable', 'unsupported', 'directory', 'missing')
    }

    assert results['runnable'] == ExecutableResolution(
        'runnable',
        runnable,
        ExecutableStatus.RUNNABLE,
    )
    assert results['unsupported'] == ExecutableResolution(
        'unsupported',
        unsupported,
        ExecutableStatus.UNSUPPORTED_SHIM,
        f'Use a real executable entry point for unsupported; '
        f'resolved unsupported Windows shim {unsupported}.',
    )
    assert results['directory'] == ExecutableResolution(
        'directory',
        None,
        ExecutableStatus.NOT_FOUND,
    )
    assert results['missing'] == ExecutableResolution(
        'missing',
        None,
        ExecutableStatus.NOT_FOUND,
    )


def test_windows_resolution_recognizes_backslash_path_separator(tmp_path: Path) -> None:
    candidate = _make_file(tmp_path / 'nested' / 'tool.EXE')
    requested = str(candidate).replace('/', '\\')

    result = resolve_executable(
        requested,
        path_value='unused',
        windows=True,
        pathext_value='.EXE',
    )

    assert result == ExecutableResolution(requested, candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize('executable', ['', 'missing', 'missing.exe'])
def test_windows_resolution_rejects_missing_tokens(
    tmp_path: Path,
    executable: str,
) -> None:
    result = resolve_executable(
        executable,
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.COM;.EXE;.BAT;.CMD;.PS1',
    )

    assert result == ExecutableResolution(executable, None, ExecutableStatus.NOT_FOUND)


@pytest.mark.parametrize(
    ('suffix', 'requested_suffix'),
    [
        ('.bat', '.BAT'),
        ('.BAT', '.bat'),
        ('.cmd', '.CMD'),
        ('.CMD', '.cmd'),
        ('.ps1', '.PS1'),
        ('.PS1', '.ps1'),
    ],
)
def test_windows_resolution_rejects_shims_in_every_letter_case(
    tmp_path: Path,
    suffix: str,
    requested_suffix: str,
) -> None:
    candidate = _make_file(tmp_path / f'hook{suffix}')
    requested = f'hook{requested_suffix}'

    result = resolve_executable(
        requested,
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.COM;.EXE;.BAT;.CMD;.PS1',
    )

    assert result.requested == requested
    assert result.resolved == candidate
    assert result.status is ExecutableStatus.UNSUPPORTED_SHIM
    assert result.correction == (
        f'Use a real executable entry point for {requested}; '
        f'resolved unsupported Windows shim {result.resolved}.'
    )


def test_windows_resolution_rejects_unapproved_explicit_pathext_script(tmp_path: Path) -> None:
    _make_file(tmp_path / 'hook.py')

    result = resolve_executable(
        'hook.py',
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.PY',
    )

    assert result == ExecutableResolution('hook.py', None, ExecutableStatus.NOT_FOUND)


def test_windows_resolution_returns_first_unsupported_shim(tmp_path: Path) -> None:
    shim = _make_file(tmp_path / 'hook.CMD')
    _make_file(tmp_path / 'hook.EXE')

    result = resolve_executable(
        'hook',
        path_value=str(tmp_path),
        windows=True,
        pathext_value='.CMD;.EXE',
    )

    assert result == ExecutableResolution(
        'hook',
        shim,
        ExecutableStatus.UNSUPPORTED_SHIM,
        f'Use a real executable entry point for hook; resolved unsupported Windows shim {shim}.',
    )


def test_windows_resolution_skips_unapproved_pathext_before_native_executable(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'bin'
    _make_file(directory / 'hook.py')
    candidate = _make_file(directory / 'hook.exe')

    result = resolve_executable(
        'hook',
        path_value=str(directory),
        windows=True,
        pathext_value='.PY;.EXE',
    )

    assert result == ExecutableResolution('hook', candidate, ExecutableStatus.RUNNABLE)


@pytest.mark.parametrize('pathext_value', [None, ''])
def test_windows_resolution_uses_default_pathext_platform_order(
    tmp_path: Path,
    pathext_value: str | None,
) -> None:
    candidate = _make_file(tmp_path / 'hook.COM')
    _make_file(tmp_path / 'hook.EXE')

    result = resolve_executable(
        'hook',
        path_value=str(tmp_path),
        windows=True,
        pathext_value=pathext_value,
    )

    assert result == ExecutableResolution('hook', candidate, ExecutableStatus.RUNNABLE)


def test_windows_resolution_uses_injected_path_and_pathext_in_order(tmp_path: Path) -> None:
    directory = tmp_path / 'bin'
    _make_file(directory / 'hook.EXE')
    candidate = _make_file(directory / 'hook.COM')

    result = resolve_executable(
        'hook',
        path_value=str(directory),
        windows=True,
        pathext_value='.COM;.EXE',
    )

    assert result == ExecutableResolution('hook', candidate, ExecutableStatus.RUNNABLE)


def test_windows_resolution_uses_path_before_pathext_precedence(tmp_path: Path) -> None:
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    candidate = _make_file(first / 'hook.EXE')
    _make_file(second / 'hook.COM')

    result = resolve_executable(
        'hook',
        path_value=f'{first};{second}',
        windows=True,
        pathext_value='.COM;.EXE',
    )

    assert result == ExecutableResolution('hook', candidate, ExecutableStatus.RUNNABLE)
