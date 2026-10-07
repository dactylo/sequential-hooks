import json
import posixpath
import re
import shlex
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import unquote

import pytest

from sequential_hooks._arguments import HookInvocation, parse_hook_argv
from sequential_hooks._executor import (
    _WINDOWS_CRASH_CODES,
    _WINDOWS_CRASH_RANGE_END,
    _WINDOWS_CRASH_RANGE_START,
    _WINDOWS_RUNTIME_CRASH_RANGE_END,
    _WINDOWS_RUNTIME_CRASH_RANGE_START,
)

_BARE_CLAUDE = re.compile(r'\bClaude\b(?!\s+Code\b)')
_COMMAND_PREFIXES = ('git ', 'make ', 'pytest ', 'sequential-hooks ', 'uv tool ')
_CONFIGURATION_LABEL = re.compile(r'(Claude Code|Codex|Antigravity) configuration(?: \([^)]*\))?:')
_EXPECTED_CODEX_COMMAND_FINDINGS = frozenset(
    {
        'child_executable_missing',
        'command_dynamic',
        'command_uninspectable',
        'duplicate_child_registration',
        'duplicate_wrapper',
        'event_forbidden',
        'host_missing',
        'host_unavailable',
        'outer_timeout_only',
        'wrong_host',
    }
)
_EXPECTED_CONFIG_CASES = frozenset(
    {
        ('Antigravity', 'json', 'PostToolUse', 'agy'),
        ('Antigravity', 'json', 'PreToolUse', 'agy'),
        ('Claude Code', 'json', 'PostToolUse', 'claude'),
        ('Claude Code', 'json', 'PreToolUse', 'claude'),
        ('Codex', 'json', 'PreToolUse', 'codex'),
        ('Codex', 'toml', 'PostToolUse', 'codex'),
    }
)
_EXPECTED_CONFIGURATION_PATHS = {
    'plugins/agy/README.md': frozenset(
        {
            '~/.gemini/config/hooks.json',
            '.agents/hooks.json',
        }
    ),
    'plugins/claude/README.md': frozenset(
        {
            '~/.claude/settings.json',
            '.claude/settings.json',
            '.claude/settings.local.json',
            '/Library/Application Support/ClaudeCode/managed-settings.json',
            '/Library/Application Support/ClaudeCode/managed-settings.d/',
            '/etc/claude-code/managed-settings.json',
            '/etc/claude-code/managed-settings.d/',
            r'C:\Program Files\ClaudeCode\managed-settings.json',
            'C:\\Program Files\\ClaudeCode\\managed-settings.d\\',
        }
    ),
    'plugins/codex/README.md': frozenset(
        {
            '~/.codex/hooks.json',
            '~/.codex/config.toml',
            '/etc/codex/hooks.json',
            '/etc/codex/config.toml',
            '.codex/hooks.json',
            '.codex/config.toml',
        }
    ),
}
_EXPECTED_PROTOCOL_SCHEMAS = (
    (
        'Claude Code',
        'PreToolUse',
        (
            'continue',
            'stopReason',
            'suppressOutput',
            'systemMessage',
            'terminalSequence',
            'hookSpecificOutput',
        ),
        (
            'hookEventName',
            'permissionDecision',
            'permissionDecisionReason',
            'additionalContext',
            'updatedInput',
        ),
    ),
    (
        'Claude Code',
        'PostToolUse',
        (
            'continue',
            'stopReason',
            'suppressOutput',
            'systemMessage',
            'terminalSequence',
            'decision',
            'reason',
            'hookSpecificOutput',
        ),
        ('hookEventName', 'additionalContext', 'updatedToolOutput'),
    ),
    (
        'Codex',
        'PreToolUse',
        ('systemMessage', 'hookSpecificOutput'),
        (
            'hookEventName',
            'permissionDecision',
            'permissionDecisionReason',
            'additionalContext',
            'updatedInput',
        ),
    ),
    (
        'Codex',
        'PostToolUse',
        ('systemMessage', 'decision', 'reason', 'continue', 'stopReason', 'hookSpecificOutput'),
        ('hookEventName', 'additionalContext'),
    ),
    ('Antigravity', 'PreToolUse', ('decision', 'reason', 'permissionOverrides'), ()),
    ('Antigravity', 'PostToolUse', (), ()),
)
_EXPECTED_PROTOCOL_SCHEMA_HEADER = (
    'Host',
    'Event',
    'Final alternatives',
    'Top-level members',
    'hookSpecificOutput members',
    'Coexistence and limits',
)
_EXPECTED_WINDOWS_CRASH_CODES = tuple(f'0x{code:08X}' for code in sorted(_WINDOWS_CRASH_CODES))
_EXPECTED_WINDOWS_CRASH_RANGES = (
    (f'0x{_WINDOWS_CRASH_RANGE_START:08X}', f'0x{_WINDOWS_CRASH_RANGE_END:08X}'),
    (f'0x{_WINDOWS_RUNTIME_CRASH_RANGE_START:08X}', f'0x{_WINDOWS_RUNTIME_CRASH_RANGE_END:08X}'),
)
_FENCE = re.compile(r'^```([^\n]*)\n(.*?)^```$', re.MULTILINE | re.DOTALL)
_HOST_VOCABULARY = {
    'plugins/agy/README.md': ('Antigravity', 'agy'),
    'plugins/claude/README.md': ('Claude Code', 'claude'),
    'plugins/codex/README.md': ('Codex', 'codex'),
}
_LINK = re.compile(r'(?<!!)\[[^\]]+\]\(([^)]+)\)')
_LOCAL_PATH = re.compile(r'(?:/Users/|/home/|[A-Za-z]:\\Users\\)')
_MARKDOWN_HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$', re.MULTILINE)
_PUBLIC_PAGE_NAMES = (
    'README.md',
    'docs/README.md',
    'docs/adding-a-host.md',
    'docs/compatibility.md',
    'docs/concepts.md',
    'docs/development.md',
    *(
        path.relative_to(Path(__file__).parents[1]).as_posix()
        for path in sorted((Path(__file__).parents[1] / 'plugins').glob('*/README.md'))
    ),
    'docs/platforms.md',
    'docs/reference/cli.md',
    'docs/reference/host-protocols.md',
    'docs/security.md',
    'docs/troubleshooting.md',
)
_RAW_PROBE_SIGNATURES = (
    ('"conversationId"', '"stepIdx"'),
    ('"session_id"', '"tool_use_id"'),
    ('"session_id"', '"transcript_path"'),
)
_ROOT = Path(__file__).parents[1]
_SECRET_VALUE = re.compile(
    r'(?:Bearer\s+[A-Za-z0-9._-]+|ghp_[A-Za-z0-9]+|sk-[A-Za-z0-9]{8,})',
    re.IGNORECASE,
)
_VISIBLE_TYPED_ID = re.compile(r'\b(?:agy|claude|codex)\b')


def _assert_config_case_set(cases: frozenset[tuple[str, str, str, str]]) -> None:
    assert cases == _EXPECTED_CONFIG_CASES


def _assert_no_raw_probe_payloads(texts: Mapping[str, str]) -> None:
    for page_name, text in texts.items():
        for signature in _RAW_PROBE_SIGNATURES:
            assert not all(marker in text for marker in signature), (
                f'{page_name}: raw probe payload signature {signature}'
            )


def _config_owner(text: str, fence_start: int) -> str | None:
    preceding_lines = text[:fence_start].rstrip().splitlines()
    if not preceding_lines:
        return None
    match = _CONFIGURATION_LABEL.fullmatch(preceding_lines[-1])
    return match.group(1) if match is not None else None


def _configuration_paths(text: str) -> frozenset[str]:
    scope = text.split('\n## ', 1)[0]
    return frozenset(
        value
        for value in re.findall(r'`([^`\n]+)`', scope)
        if value.startswith(('~/', '.', '/', 'C:\\'))
    )


def _assert_configuration_paths(texts: Mapping[str, str]) -> None:
    actual = {
        page_name: _configuration_paths(texts[page_name])
        for page_name in _EXPECTED_CONFIGURATION_PATHS
    }
    assert actual == _EXPECTED_CONFIGURATION_PATHS


def _direct_wrapper_argvs(mapping: Mapping[object, object]) -> Iterator[tuple[str, ...]]:
    command = mapping.get('command')
    args = mapping.get('args')
    if command == 'sequential-hooks' and isinstance(args, Sequence) and not isinstance(args, str):
        argument_values = cast('Sequence[object]', args)
        arguments = tuple(item for item in argument_values if isinstance(item, str))
        if len(arguments) == len(argument_values):
            yield ('sequential-hooks', *arguments)
    elif isinstance(command, str) and command.startswith('sequential-hooks '):
        yield tuple(shlex.split(command))
    for key in ('commandWindows', 'command_windows'):
        override = mapping.get(key)
        if isinstance(override, str) and override.startswith('sequential-hooks '):
            yield tuple(shlex.split(override))


def _event_wrapper_argvs(
    value: object,
    event: str | None = None,
) -> Iterator[tuple[str | None, tuple[str, ...]]]:
    if isinstance(value, Mapping):
        mapping = cast('Mapping[object, object]', value)
        for argv in _direct_wrapper_argvs(mapping):
            yield event, argv
        for key, item in mapping.items():
            child_event = key if key in {'PreToolUse', 'PostToolUse'} else event
            yield from _event_wrapper_argvs(item, cast('str | None', child_event))
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for item in cast('Sequence[object]', value):
            yield from _event_wrapper_argvs(item, event)


def _fences(text: str) -> Iterator[tuple[str, str, int]]:
    for match in _FENCE.finditer(text):
        yield match.group(1).strip(), match.group(2), match.start()


def _hook_commands(texts: Mapping[str, str]) -> Iterator[tuple[str, HookInvocation]]:
    for page_name, text in texts.items():
        for language, body, _start in _fences(text):
            if language != 'text':
                continue
            for line in body.splitlines():
                if not line.startswith('sequential-hooks ') or line.startswith(
                    'sequential-hooks doctor '
                ):
                    continue
                argv = shlex.split(line)
                yield page_name, parse_hook_argv(argv[1:])


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON key: {key}')
        value[key] = item
    return value


def _markdown_section(text: str, heading: str, *, level: int = 2) -> str:
    marker = f'{"#" * level} {heading}\n'
    section = text.split(marker, 1)[1]
    return section.split(f'\n{"#" * level} ', 1)[0]


def _assert_windows_crash_values(text: str) -> None:
    windows = _markdown_section(text, 'Native Windows')
    ranges = tuple(re.findall(r'`(0x[0-9A-F]+)\.\.(0x[0-9A-F]+)`', windows))
    crash_codes = tuple(re.findall(r'`(0x8000000[1-9A-F])`', windows))
    assert ranges == _EXPECTED_WINDOWS_CRASH_RANGES
    assert crash_codes == _EXPECTED_WINDOWS_CRASH_CODES


def _parse_json(body: str) -> object:

    def reject_constant(value: str) -> object:
        raise ValueError(f'non-standard JSON constant: {value}')

    return json.loads(
        body,
        object_pairs_hook=_json_object,
        parse_constant=reject_constant,
    )


def _assert_configuration_examples(texts: Mapping[str, str]) -> None:
    cases: set[tuple[str, str, str, str]] = set()
    owners: set[str] = set()
    for page_name, text in texts.items():
        # Each additional plugin owns its native configuration contract tests.
        if page_name.startswith('plugins/') and page_name not in _HOST_VOCABULARY:
            continue
        for language, body, start in _fences(text):
            if language not in {'json', 'toml'}:
                continue
            if language == 'toml' and 'entry-points' in tomllib.loads(body):
                continue
            owner = _config_owner(text, start)
            assert owner is not None
            owners.add(owner)
            value = _parse_json(body) if language == 'json' else tomllib.loads(body)
            event_commands = list(_event_wrapper_argvs(value))
            assert event_commands
            for event, argv in event_commands:
                assert event in {'PreToolUse', 'PostToolUse'}
                invocation = parse_hook_argv(argv[1:])
                cases.add((owner, language, event, invocation.host))
    _assert_config_case_set(frozenset(cases))
    assert owners == {'Antigravity', 'Claude Code', 'Codex'}


def _public_texts() -> dict[str, str]:
    missing = [name for name in _PUBLIC_PAGE_NAMES if not (_ROOT / name).is_file()]
    if missing:
        pytest.skip(f'public guide tree is incomplete: {missing}')
    return {name: (_ROOT / name).read_text() for name in _PUBLIC_PAGE_NAMES}


def _strip_code_and_link_targets(text: str) -> str:
    without_fences = _FENCE.sub('', text)
    without_code = re.sub(r'`[^`\n]+`', '', without_fences)
    return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', without_code)


def _heading_anchors(text: str) -> frozenset[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for match in _MARKDOWN_HEADING.finditer(text):
        label = _strip_code_and_link_targets(match.group(1)).lower()
        base = re.sub(r'[^\w -]', '', label).strip().replace(' ', '-')
        count = counts.get(base, 0)
        anchor = base if count == 0 else f'{base}-{count}'
        counts[base] = count + 1
        anchors.add(anchor)
    return frozenset(anchors)


def _assert_links_resolve(texts: Mapping[str, str]) -> None:
    anchors = {page_name: _heading_anchors(text) for page_name, text in texts.items()}
    for page_name, text in texts.items():
        for target in _LINK.findall(text):
            if target.startswith(('http://', 'https://', 'mailto:')):
                continue
            path_text, separator, fragment = target.partition('#')
            if path_text:
                target_page = posixpath.normpath(
                    posixpath.join(posixpath.dirname(page_name), path_text)
                )
            else:
                target_page = page_name
            if target_page not in texts:
                target_path = (_ROOT / target_page).resolve()
                assert not separator, f'{page_name}: unresolved link {target}'
                assert target_path.is_relative_to(_ROOT), f'{page_name}: unresolved link {target}'
                assert target_path.suffix.lower() not in {'.md', '.markdown'}, (
                    f'{page_name}: unresolved link {target}'
                )
                assert target_path.is_file(), f'{page_name}: unresolved link {target}'
                continue
            if separator:
                normalized_fragment = unquote(fragment)
                assert normalized_fragment in anchors[target_page], (
                    f'{page_name}: unresolved heading {target}'
                )


def _table_rows_from_section(section: str) -> list[tuple[str, ...]]:
    table_lines: list[str] = []
    table_started = False
    for line in section.lstrip().splitlines():
        if not table_started and not line.startswith('|'):
            continue
        if not line.startswith('|'):
            break
        table_started = True
        table_lines.append(line)
    rows: list[tuple[str, ...]] = [
        tuple(cell.strip() for cell in line.strip('|').split('|')) for line in table_lines
    ]
    return [row for row in rows if not all(set(cell) <= {'-', ':'} for cell in row)]


def _table_rows(text: str, heading: str) -> list[tuple[str, ...]]:
    marker = f'## {heading}\n'
    assert marker in text, f'missing section: {heading}'
    section = text.split(marker, 1)[1]
    return _table_rows_from_section(section)


def _protocol_schema_contract(
    text: str,
) -> tuple[tuple[str, str, frozenset[str], frozenset[str]], ...]:
    rows = _table_rows(text, 'Structured final response schemas')
    assert rows[0] == _EXPECTED_PROTOCOL_SCHEMA_HEADER
    return tuple(
        (
            host,
            event,
            frozenset(re.findall(r'`([^`]+)`', top_level)),
            frozenset(re.findall(r'`([^`]+)`', nested)),
        )
        for host, event, _alternatives, top_level, nested, _rules in rows[1:]
    )


def _assert_protocol_contract(text: str) -> None:
    assert _protocol_schema_contract(text) == tuple(
        (host, event, frozenset(top_level), frozenset(nested))
        for host, event, top_level, nested in _EXPECTED_PROTOCOL_SCHEMAS
    )


def _typed_context(text: str) -> str:
    fence_bodies = [body for _language, body, _start in _fences(text)]
    inline_code = re.findall(r'`([^`\n]+)`', _FENCE.sub('', text))
    return '\n'.join((*fence_bodies, *inline_code))


def _assert_product_vocabulary(texts: Mapping[str, str]) -> None:
    visible_prose = '\n'.join(_strip_code_and_link_targets(text) for text in texts.values())
    assert _BARE_CLAUDE.search(visible_prose) is None
    assert _VISIBLE_TYPED_ID.search(visible_prose) is None
    for page_name, (product_name, host_id) in _HOST_VOCABULARY.items():
        text = texts[page_name]
        visible_page = _strip_code_and_link_targets(text)
        typed_context = _typed_context(text)
        assert product_name in visible_page
        assert re.search(rf'\b{re.escape(host_id)}\b', typed_context)


def test_additional_plugin_guide_does_not_change_existing_configuration_contracts() -> None:
    texts = _public_texts()
    texts['plugins/example/README.md'] = (
        'Example configuration:\n\n```json\n'
        '{"hooks":{"BeforeTool":[{"command":"sequential-hooks --agent example child"}]}}\n'
        '```\n'
    )
    _assert_configuration_examples(texts)


def test_all_public_pages_exist() -> None:
    missing = [name for name in _PUBLIC_PAGE_NAMES if not (_ROOT / name).is_file()]

    assert missing == []


def test_code_fences_are_parseable_and_commands_are_literal() -> None:
    texts = _public_texts()
    json_count = 0
    toml_count = 0
    for text in texts.values():
        for language, body, _start in _fences(text):
            if any(line.startswith(_COMMAND_PREFIXES) for line in body.splitlines()):
                assert language in {'text', 'sh'}
                assert '<' not in body
                assert '>' not in body
            if language == 'json':
                _parse_json(body)
                json_count += 1
            elif language == 'toml':
                tomllib.loads(body)
                toml_count += 1

    assert json_count > 0
    assert toml_count > 0


def test_codex_command_findings_are_documented() -> None:
    texts = _public_texts()
    guidance = '\n'.join(
        texts[path] for path in ('plugins/codex/README.md', 'docs/troubleshooting.md')
    )

    assert {
        code for code in _EXPECTED_CODEX_COMMAND_FINDINGS if f'`{code}`' not in guidance
    } == set()


def test_configuration_examples_name_their_owner_and_explicit_host() -> None:
    _assert_configuration_examples(_public_texts())


def test_configuration_path_sets_are_exact() -> None:
    _assert_configuration_paths(_public_texts())


def test_hook_commands_parse_and_antigravity_commands_name_the_event() -> None:
    commands = list(_hook_commands(_public_texts()))

    assert commands
    for page_name, invocation in commands:
        if invocation.host == 'agy':
            assert invocation.event in {'PreToolUse', 'PostToolUse'}, page_name
        elif invocation.host in {'claude', 'codex'}:
            assert invocation.event is None, page_name


@pytest.mark.parametrize('reordered', [False, True])
def test_makefile_formats_and_checks_exactly_the_public_markdown_tree(reordered: bool) -> None:
    _public_texts()
    text = (_ROOT / 'Makefile').read_text()
    if reordered:
        text = text.replace('README.md docs/README.md', 'docs/README.md README.md', 1)
        text = text.replace(
            '\tbiome format --write .\n\tmdformat $(MARKDOWN_FILES)',
            '\tmdformat $(MARKDOWN_FILES)\n\n\tbiome format --write .',
            1,
        ).replace(
            '\tbiome format .\n\tmdformat --check $(MARKDOWN_FILES)',
            '\tmdformat --check $(MARKDOWN_FILES)\n\n\tbiome format .',
            1,
        )
    lines = text.splitlines()
    markdown_line = next(line for line in lines if line.startswith('MARKDOWN_FILES := '))
    markdown_line = markdown_line.replace(
        '$(wildcard plugins/*/README.md)',
        ' '.join(
            path.relative_to(_ROOT).as_posix()
            for path in sorted((_ROOT / 'plugins').glob('*/README.md'))
        ),
    )
    markdown_files = shlex.split(markdown_line.removeprefix('MARKDOWN_FILES := '))
    assert set(markdown_files) == set(_PUBLIC_PAGE_NAMES)

    recipes: dict[str, list[str]] = {}
    target = ''
    for line in lines:
        if line.startswith('\t'):
            recipes.setdefault(target, []).append(line)
        elif line and not line.startswith('#'):
            target = line.split(':', maxsplit=1)[0]

    assert set(recipes['format']) >= {
        '\tbiome format --write .',
        '\t$(DEV_PYTHON) -m mdformat $(MARKDOWN_FILES)',
        '\t$(VENV_BIN)/pyproject-fmt pyproject.toml',
    }

    assert set(recipes['format-check']) >= {
        '\tbiome format .',
        '\t$(DEV_PYTHON) -m mdformat --check $(MARKDOWN_FILES)',
        '\t$(VENV_BIN)/pyproject-fmt --check pyproject.toml',
    }


def test_product_vocabulary_accepts_wrapped_claude_code() -> None:
    assert _BARE_CLAUDE.search('Claude\nCode') is None
    assert _BARE_CLAUDE.search('Claude integration') is not None


def test_protocol_member_enumeration_order_is_not_significant() -> None:
    text = _public_texts()['docs/reference/host-protocols.md']
    text = text.replace('`suppressOutput`, `systemMessage`', '`systemMessage`, `suppressOutput`')
    text = text.replace(
        '`additionalContext`, `updatedInput`', '`updatedInput`, `additionalContext`'
    )

    _assert_protocol_contract(text)


def test_protocol_members_are_documented() -> None:
    _assert_protocol_contract(_public_texts()['docs/reference/host-protocols.md'])


def test_public_link_to_existing_repository_file_resolves() -> None:
    _assert_links_resolve({'docs/page.md': '# Page\n\n[CI](../.github/workflows/ci.yml)\n'})


def test_public_links_resolve() -> None:
    _assert_links_resolve(_public_texts())


def test_public_prose_uses_safe_vocabulary_and_contains_no_private_data() -> None:
    texts = _public_texts()
    combined = '\n'.join(texts.values())

    assert combined.isascii()
    assert _LOCAL_PATH.search(combined) is None
    assert _SECRET_VALUE.search(combined) is None
    _assert_no_raw_probe_payloads(texts)
    _assert_product_vocabulary(texts)
    assert 'provider-aware' not in combined
    assert 'provider protocol' not in combined
    assert 'supported but untested' not in combined.lower()
    assert re.search(r'\bsupported on\b', combined, re.IGNORECASE) is None
    assert 'sequential-hooks has no configuration file.' in combined


def test_rejects_bad_relative_links_and_anchors() -> None:
    with pytest.raises(AssertionError, match='unresolved link'):
        _assert_links_resolve({'docs/page.md': '# Page\n\n[missing](missing.md)\n'})
    with pytest.raises(AssertionError, match='unresolved heading'):
        _assert_links_resolve({'docs/page.md': '# Page\n\n[missing](#other)\n'})


def test_rejects_markdown_link_missing_from_page_inventory() -> None:
    with pytest.raises(AssertionError, match='unresolved link'):
        _assert_links_resolve({'docs/page.md': '# Page\n\n[development](development.md)\n'})


def test_rejects_missing_and_wrong_configuration_cases() -> None:
    missing = _EXPECTED_CONFIG_CASES - {('Claude Code', 'json', 'PostToolUse', 'claude')}
    wrong = _EXPECTED_CONFIG_CASES - {('Codex', 'toml', 'PostToolUse', 'codex')} | {
        ('Codex', 'toml', 'PreToolUse', 'codex')
    }

    with pytest.raises(AssertionError):
        _assert_config_case_set(frozenset(missing))
    with pytest.raises(AssertionError):
        _assert_config_case_set(frozenset(wrong))


def test_rejects_missing_configuration_path() -> None:
    texts = _public_texts()
    texts['plugins/claude/README.md'] = texts['plugins/claude/README.md'].replace(
        '- Local project: `.claude/settings.local.json`.\n',
        '',
    )

    with pytest.raises(AssertionError):
        _assert_configuration_paths(texts)


def test_rejects_missing_repository_file_link() -> None:
    with pytest.raises(AssertionError, match='unresolved link'):
        _assert_links_resolve(
            {'docs/page.md': '# Page\n\n[missing](../.github/workflows/missing.yml)\n'}
        )


def test_rejects_omitted_protocol_member() -> None:
    text = _public_texts()['docs/reference/host-protocols.md']
    mutated = text.replace('`terminalSequence`, ', '', 1)

    with pytest.raises(AssertionError):
        _assert_protocol_contract(mutated)


def test_rejects_product_vocabulary_drift() -> None:
    texts = _public_texts()
    missing_product = dict(texts)
    missing_product['README.md'] = missing_product['README.md'].replace(
        '[Claude Code]',
        '[Claude]',
        1,
    )
    assert '[Claude]' in missing_product['README.md']
    visible_id = dict(texts)
    visible_id['plugins/claude/README.md'] = (
        f'claude is visible prose.\n{visible_id["plugins/claude/README.md"]}'
    )

    with pytest.raises(AssertionError):
        _assert_product_vocabulary(missing_product)
    with pytest.raises(AssertionError):
        _assert_product_vocabulary(visible_id)


def test_rejects_raw_probe_payloads() -> None:
    raw_payload = (
        '{"session_id":"session-1","transcript_path":"transcript.jsonl","tool_use_id":"tool-1"}'
    )

    with pytest.raises(AssertionError, match='raw probe payload'):
        _assert_no_raw_probe_payloads({'README.md': raw_payload})


def test_rejects_wrong_windows_crash_value() -> None:
    text = _public_texts()['docs/platforms.md']
    mutated = text.replace('`0x80000004`', '`0x80000005`', 1)

    with pytest.raises(AssertionError):
        _assert_windows_crash_values(mutated)


def test_windows_crash_values_are_documented() -> None:
    _assert_windows_crash_values(_public_texts()['docs/platforms.md'])
