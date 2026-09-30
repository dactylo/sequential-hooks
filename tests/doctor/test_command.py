import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from sequential_hooks._doctor._command import inspect_command, redact_command
from sequential_hooks._doctor._types import (
    ConfigSource,
    ContractError,
    LaunchSemantics,
    Registration,
    SourceKind,
)
from tests._distributions import register_distribution


def _registration(
    command: str = 'sequential-hooks --agent codex child',
    **changes: object,
) -> Registration:
    if 'semantics' in changes:
        changes['launch_semantics'] = changes.pop('semantics')
    if 'windows_semantics' in changes:
        changes['windows_launch_semantics'] = changes.pop('windows_semantics')
    registration = Registration(
        source=ConfigSource(
            path=Path('/safe/config.json'),
            kind=SourceKind.USER,
            exists=True,
        ),
        event='PostToolUse',
        matcher=None,
        command=command,
        args=None,
        launch_semantics=LaunchSemantics.POSIX_SHELL,
        command_windows=None,
        windows_launch_semantics=None,
        timeout=30.0,
        enabled=True,
        type=None,
    )
    return replace(registration, **changes)


def test_absolute_wrapper_and_non_wrapper_are_distinguished(tmp_path: Path) -> None:
    wrapper = inspect_command(
        _registration(
            str(tmp_path / 'sequential-hooks'),
            args=('--agent', 'claude', 'child'),
            semantics=LaunchSemantics.DIRECT_ARGV,
        ),
        windows=False,
    )
    child = inspect_command(
        _registration(
            str(tmp_path / 'not-sequential-hooks'),
            args=('--agent', 'claude', 'child'),
            semantics=LaunchSemantics.DIRECT_ARGV,
        ),
        windows=False,
    )

    assert wrapper.invocation is not None
    assert child.argv is not None
    assert child.invocation is None
    assert child.error is None


def test_direct_argv_preserves_literal_child_arguments() -> None:
    registration = _registration(
        'sequential-hooks',
        args=('--agent', 'codex', 'child argument', '$(not syntax)', 'a;b'),
        semantics=LaunchSemantics.DIRECT_ARGV,
    )

    result = inspect_command(registration, windows=False)

    assert result.argv == (
        'sequential-hooks',
        '--agent',
        'codex',
        'child argument',
        '$(not syntax)',
        'a;b',
    )
    assert result.invocation is not None
    assert result.invocation.steps[0].argv == ('child argument', '$(not syntax)', 'a;b')


def test_direct_argv_requires_loader_supplied_args() -> None:
    result = inspect_command(
        _registration(semantics=LaunchSemantics.DIRECT_ARGV),
        windows=False,
    )

    assert result.error == 'direct argv registration is missing args'


def test_inspection_never_starts_or_invokes_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / 'marker'

    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail('doctor started a process')

    monkeypatch.setattr(subprocess, 'Popen', fail)
    monkeypatch.setattr(subprocess, 'run', fail)
    monkeypatch.setattr(os, 'system', fail)
    monkeypatch.setattr(os, 'popen', fail)

    inspect_command(
        _registration(f'sequential-hooks --agent codex "$(touch {marker})"'),
        windows=False,
    )

    assert not marker.exists()


def test_malformed_wrapper_returns_safe_error() -> None:
    result = inspect_command(
        _registration('sequential-hooks --agent codex --bogus super-secret'),
        windows=False,
    )

    assert result.invocation is None
    assert result.error is not None
    assert 'super-secret' not in result.error


def test_posix_shell_comment_before_newline_remains_dynamic() -> None:
    result = inspect_command(
        _registration('sequential-hooks --agent codex child # ignored\nsecond-command'),
        windows=False,
    )

    assert result.dynamic is True
    assert result.argv is None
    assert result.invocation is None


@pytest.mark.parametrize(
    'syntax',
    [
        '$(touch marker)',
        '`touch marker`',
        '$SECRET',
        '"$SECRET"',
        '"`touch marker`"',
        'one | two',
        'one > file',
        'one; two',
        'one && two',
        'one || two',
        'one\ntwo',
        'child *',
        'child ?',
        'child [ab]',
        'child ~',
    ],
)
def test_posix_shell_dynamic_syntax_is_not_parsed(syntax: str) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {syntax}'), windows=False
    )

    assert result.dynamic is True
    assert result.argv is None
    assert result.invocation is None


@pytest.mark.parametrize(
    ('suffix', 'expected'),
    [
        ('arg # ignored --agent agy', 'arg'),
        ('word#literal', 'word#literal'),
        (r'\#literal', '#literal'),
        ("'#literal'", '#literal'),
        ('"#literal"', '#literal'),
    ],
)
def test_posix_shell_comment_boundary_preserves_literal_hashes(
    suffix: str,
    expected: str,
) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {suffix}'), windows=False
    )

    assert result.argv == ('sequential-hooks', '--agent', 'codex', 'child', expected)
    assert result.invocation is not None
    assert result.invocation.steps[0].argv == ('child', expected)


def test_posix_shell_leading_nonstandalone_bang_remains_literal() -> None:
    result = inspect_command(_registration('!literal'), windows=False)

    assert result.dynamic is False
    assert result.argv == ('!literal',)


@pytest.mark.parametrize(
    ('literal', 'expected'),
    [
        ("'$HOME'", '$HOME'),
        ("'`pwd`'", '`pwd`'),
    ],
)
def test_posix_shell_single_quoted_expansion_markers_remain_literal(
    literal: str,
    expected: str,
) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {literal}'),
        windows=False,
    )

    assert result.dynamic is False
    assert result.invocation is not None
    assert result.invocation.steps[0].argv == ('child', expected)


@pytest.mark.parametrize(
    'command',
    [
        'FOO=bar sequential-hooks --agent codex child',
        '! sequential-hooks --agent codex child',
        '( sequential-hooks --agent codex child )',
    ],
)
def test_posix_shell_prefixes_are_dynamic(command: str) -> None:
    result = inspect_command(_registration(command), windows=False)

    assert result.dynamic is True
    assert result.argv is None
    assert result.invocation is None
    assert result.error == 'command contains dynamic shell syntax'


@pytest.mark.parametrize(
    ('literal', 'expected'),
    [
        ("'!'", '!'),
        (r'\!', '!'),
        ("'('", '('),
        (r'\(', '('),
        ("')'", ')'),
        (r'\)', ')'),
    ],
)
def test_posix_shell_quoted_or_escaped_control_tokens_remain_literal(
    literal: str,
    expected: str,
) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {literal}'),
        windows=False,
    )

    assert result.dynamic is False
    assert result.invocation is not None
    assert result.invocation.steps[0].argv == ('child', expected)


@pytest.mark.parametrize('literal', ["'*'", '"?"', "'[ab]'", r'\*'])
def test_posix_shell_quoted_or_escaped_globs_remain_literal(literal: str) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {literal}'),
        windows=False,
    )

    assert result.dynamic is False
    assert result.invocation is not None


@pytest.mark.parametrize('literal', ['!', '!literal'])
def test_posix_shell_unquoted_bang_arguments_remain_literal(literal: str) -> None:
    result = inspect_command(
        _registration(f'sequential-hooks --agent codex child {literal}'),
        windows=False,
    )

    assert result.dynamic is False
    assert result.invocation is not None
    assert result.invocation.steps[0].argv == ('child', literal)


def test_posix_wrapper_recognition_remains_case_sensitive() -> None:
    result = inspect_command(
        _registration(
            'Sequential-Hooks',
            semantics=LaunchSemantics.DIRECT_ARGV,
            args=('--agent', 'codex', 'child'),
        ),
        windows=False,
    )

    assert result.invocation is None


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        (
            'sequential-hooks --agent codex --event PostToolUse child --',
            ContractError.EVENT_FORBIDDEN,
        ),
        ('sequential-hooks --agent agy child --', ContractError.EVENT_MISSING),
        ('sequential-hooks child', ContractError.HOST_MISSING),
        ('sequential-hooks --agent agy --event Other child --', ContractError.WRONG_EVENT),
        ('sequential-hooks --agent unknown child --', ContractError.WRONG_HOST),
    ],
)
def test_recognized_wrapper_contract_errors_are_typed(
    command: str,
    expected: ContractError,
) -> None:
    result = inspect_command(_registration(command), windows=False)

    assert result.contract_error is expected
    assert result.error is not None
    assert not result.error.startswith('wrapper_contract:')


def test_redaction_replaces_arbitrary_leading_environment_block() -> None:
    rendered = redact_command(
        _registration(
            'PUBLIC_CONTEXT=private-sentinel OTHER_KEY=other-sentinel '
            'sequential-hooks --agent codex child'
        )
    )

    assert rendered == '[REDACTED] environment command'
    assert 'PUBLIC_CONTEXT' not in rendered
    assert 'private-sentinel' not in rendered
    assert 'OTHER_KEY' not in rendered
    assert 'other-sentinel' not in rendered


def test_short_agent_option_is_inspected() -> None:
    result = inspect_command(_registration('sequential-hooks -a codex child'), windows=False)

    assert result.error is None
    assert result.invocation is not None
    assert result.invocation.host == 'codex'


def test_short_event_option_is_inspected() -> None:
    result = inspect_command(
        _registration('sequential-hooks -a agy -e PreToolUse child'), windows=False
    )

    assert result.error is None
    assert result.invocation is not None
    assert result.invocation.event == 'PreToolUse'


def test_unrecognized_wrapper_parse_error_remains_text() -> None:
    result = inspect_command(
        _registration('sequential-hooks --bogus --agent codex child'),
        windows=False,
    )

    assert result.contract_error is None
    assert result.error is not None
    assert not result.error.startswith('wrapper_contract:')


@pytest.mark.parametrize(
    'command',
    [
        'sequential-hooks --agent codex --event PostToolUse child',
        'sequential-hooks --agent agy child',
        'sequential-hooks --agent agy --event Other child',
        'sequential-hooks --agent unknown child',
    ],
)
def test_well_formed_contract_violations_parse_for_invocation_analysis(command: str) -> None:
    result = inspect_command(_registration(command), windows=False)

    assert result.invocation is not None
    assert result.contract_error is None
    assert result.error is None


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        ('mytool API_KEY=sk-1234', 'mytool API_KEY=[REDACTED]'),
        ('mytool OPENAI_API_KEY=sk-1234', 'mytool OPENAI_API_KEY=[REDACTED]'),
        ('mytool APIKEY=sk-1234', 'mytool APIKEY=[REDACTED]'),
        ('mytool --api_key sk-1234', 'mytool --api_key [REDACTED]'),
        ('mytool --api_key=sk-1234', 'mytool --api_key=[REDACTED]'),
        ('mytool --api.key=sk-1234', 'mytool --api.key=[REDACTED]'),
        ('mytool --apikey sk-1234', 'mytool --apikey [REDACTED]'),
        ('mytool --apiKey sk-1234', 'mytool --apiKey [REDACTED]'),
        ('mytool --credentials-file=/x', 'mytool --credentials-file=[REDACTED]'),
        ('mytool --credentials /x', 'mytool --credentials [REDACTED]'),
        ('mytool --tokens=abc', 'mytool --tokens=[REDACTED]'),
        ('mytool --secrets=abc', 'mytool --secrets=[REDACTED]'),
        ('mytool --passwords=abc', 'mytool --passwords=[REDACTED]'),
        ('mytool --api-keys=abc', 'mytool --api-keys=[REDACTED]'),
        ('env API_KEY=sk-1234 mytool', 'env API_KEY=[REDACTED] mytool'),
        ('mytool -k sk-1234', 'mytool -k sk-1234'),
        ('mytool --no-tokens on', 'mytool --no-tokens on'),
        ('mytool --tokenizer bert', 'mytool --tokenizer bert'),
        ('runner --api-token=one tail', 'runner --api-token=[REDACTED] tail'),
        ('runner --api-token one tail', 'runner --api-token [REDACTED] tail'),
        ('runner --client-secret=two tail', 'runner --client-secret=[REDACTED] tail'),
        ('runner --client-secret two tail', 'runner --client-secret [REDACTED] tail'),
        ('runner --password=three tail', 'runner --password=[REDACTED] tail'),
        ('runner --password three tail', 'runner --password [REDACTED] tail'),
        ('runner --credential=four tail', 'runner --credential=[REDACTED] tail'),
        ('runner --credential four tail', 'runner --credential [REDACTED] tail'),
        ('runner --api-key=five tail', 'runner --api-key=[REDACTED] tail'),
        ('runner --api-key five tail', 'runner --api-key [REDACTED] tail'),
        (
            'runner --api-token="top secret" tail',
            'runner --api-token=[REDACTED] tail',
        ),
        (
            "runner --api-token='top secret' tail",
            'runner --api-token=[REDACTED] tail',
        ),
        (
            r'runner --api-token=top\ secret tail',
            'runner --api-token=[REDACTED] tail',
        ),
        (
            'runner --api-token  "top secret"\ttail',
            'runner --api-token  [REDACTED]\ttail',
        ),
        (
            "runner --api-token\t'top secret'  tail",
            'runner --api-token\t[REDACTED]  tail',
        ),
        (
            r'runner --api-token top\ secret tail',
            'runner --api-token [REDACTED] tail',
        ),
        (
            'runner "--api-token=top secret" tail',
            'runner "--api-token=[REDACTED]" tail',
        ),
        (
            "runner '--api-token=top secret' tail",
            "runner '--api-token=[REDACTED]' tail",
        ),
        (
            "runner --api-'token'='top secret' tail",
            "runner --api-'token'=[REDACTED] tail",
        ),
        (
            "runner --api-'token='topsecret tail",
            "runner --api-'token='[REDACTED] tail",
        ),
        (
            "runner --api-'token' 'top secret' tail",
            "runner --api-'token' [REDACTED] tail",
        ),
        (
            r'runner --api-tok\en=top\ secret tail',
            r'runner --api-tok\en=[REDACTED] tail',
        ),
        (
            'runner --api-to\\\nken=topsecret tail',
            'runner --api-to\\\nken=[REDACTED] tail',
        ),
        (
            "runner ACCESS_TO'KEN'='top secret' tail",
            "runner ACCESS_TO'KEN'=[REDACTED] tail",
        ),
        (
            "runner ACCESS_TO'KEN='topsecret tail",
            "runner ACCESS_TO'KEN='[REDACTED] tail",
        ),
        (
            'runner ACCESS_TO\\\nKEN=topsecret tail',
            'runner ACCESS_TO\\\nKEN=[REDACTED] tail',
        ),
        (
            "runner --no-'token'='visible secret' tail",
            "runner --no-'token'='visible secret' tail",
        ),
        (
            r"runner --tokeniz\er='visible secret' tail",
            r"runner --tokeniz\er='visible secret' tail",
        ),
        (
            'runner --tokeni\\\nzer=visible tail',
            'runner --tokeni\\\nzer=visible tail',
        ),
        (
            'runner "--api-token secret" --mode safe',
            'runner "--api-token secret" --mode safe',
        ),
        ('runner --api-key= tail', 'runner --api-key=[REDACTED] tail'),
        ('runner --api-key', 'runner --api-key'),
        (
            'runner --api-token --client-secret private',
            'runner --api-token --client-secret [REDACTED]',
        ),
        (
            'runner --tokenizer public --no-token visible',
            'runner --tokenizer public --no-token visible',
        ),
        (
            'runner --tokenizer=public --no-token=visible',
            'runner --tokenizer=public --no-token=visible',
        ),
        ('runner --Client-Secret\tprivate  tail', 'runner --Client-Secret\t[REDACTED]  tail'),
        ('runner ACCESS_TOKEN=private tail', 'runner ACCESS_TOKEN=[REDACTED] tail'),
        (
            'runner ACCESS_TOKEN="top secret" tail',
            'runner ACCESS_TOKEN=[REDACTED] tail',
        ),
        (
            "runner ACCESS_TOKEN='top secret' tail",
            'runner ACCESS_TOKEN=[REDACTED] tail',
        ),
        (
            r'runner ACCESS_TOKEN=top\ secret tail',
            'runner ACCESS_TOKEN=[REDACTED] tail',
        ),
    ],
)
def test_redaction_uses_exact_secret_option_grammar(command: str, expected: str) -> None:
    assert redact_command(_registration(command)) == expected


@pytest.mark.parametrize(
    'semantics',
    [
        LaunchSemantics.WINDOWS_SHELL,
        LaunchSemantics.HOST_TOKENIZER,
        LaunchSemantics.UNKNOWN,
    ],
)
def test_unverified_grammars_are_uninspectable(semantics: LaunchSemantics) -> None:
    result = inspect_command(_registration(args=('child',), semantics=semantics), windows=False)

    assert result.argv is None
    assert result.dynamic is False
    assert result.error is not None


def test_windows_uses_only_documented_windows_form() -> None:
    missing = inspect_command(_registration(), windows=True)
    direct = inspect_command(
        _registration(
            command_windows='sequential-hooks.exe',
            windows_semantics=LaunchSemantics.DIRECT_ARGV,
            args=('--agent', 'codex', 'child'),
        ),
        windows=True,
    )

    assert missing.argv is None
    assert missing.error == 'registration has no documented Windows launch form'
    assert direct.argv is not None
    assert direct.argv[0] == 'sequential-hooks.exe'


@pytest.mark.parametrize(
    'command_windows',
    [
        r'C:\Tools\sequential-hooks.exe',
        r'C:\Tools\SEQUENTIAL-HOOKS.EXE',
        'Sequential-Hooks.EXE',
    ],
)
def test_windows_wrapper_recognition_is_path_and_case_insensitive(
    command_windows: str,
) -> None:
    result = inspect_command(
        _registration(
            command_windows=command_windows,
            windows_semantics=LaunchSemantics.DIRECT_ARGV,
            args=('--agent', 'codex', 'child'),
        ),
        windows=True,
    )

    assert result.invocation is not None


@pytest.mark.parametrize(
    ('event', 'expected'), [('BeforeTool', None), ('Other', ContractError.WRONG_EVENT)]
)
def test_malformed_wrapper_uses_the_selected_host_event_domain(
    distribution_root: Path,
    event: str,
    expected: ContractError | None,
) -> None:
    register_distribution(
        distribution_root,
        'sequential-hooks-host-example',
        {'example': 'tests.hosts_example.host:ExampleHost'},
    )
    inspected = inspect_command(
        _registration(f'sequential-hooks --agent example --event {event} child --'), windows=False
    )

    assert inspected.error is not None
    assert inspected.contract_error == expected
