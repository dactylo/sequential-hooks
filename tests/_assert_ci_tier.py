import sys
import xml.parsers.expat
from pathlib import Path

_COUNT_KEYS = ('tests', 'skipped', 'failures', 'errors')


def _read_counts(path: Path) -> tuple[int, int, int, int]:
    suites = 0
    totals = dict.fromkeys(_COUNT_KEYS, 0)

    def handle_start(name: str, attributes: dict[str, str]) -> None:
        nonlocal suites
        if name != 'testsuite':
            return
        suites += 1
        if not set(_COUNT_KEYS).issubset(attributes):
            msg = 'test suite has missing count attributes'
            raise ValueError(msg)
        for key in _COUNT_KEYS:
            value = attributes[key]
            if not value.isascii() or not value.isdecimal():
                msg = 'test suite has invalid count attributes'
                raise ValueError(msg)
            totals[key] += int(value)

    parser = xml.parsers.expat.ParserCreate()
    parser.StartElementHandler = handle_start
    assert parser.StartElementHandler is handle_start
    try:
        with path.open('rb') as report_file:
            parser.ParseFile(report_file)
    except OSError as exc:
        msg = 'cannot read report'
        raise ValueError(msg) from exc
    except xml.parsers.expat.ExpatError as exc:
        msg = f'malformed XML: {exc}'
        raise ValueError(msg) from exc
    if suites == 0:
        msg = 'report contains no test suites'
        raise ValueError(msg)
    return (
        totals['tests'],
        totals['skipped'],
        totals['failures'],
        totals['errors'],
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: _assert_ci_tier.py JUNIT_XML', file=sys.stderr)
        return 2
    try:
        tests, skipped, failures, errors = _read_counts(Path(argv[0]))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'invalid required-tier report: {exc}', file=sys.stderr)
        return 2
    summary = f'tests={tests} skipped={skipped} failures={failures} errors={errors}'
    if tests == 0 or skipped != 0 or failures != 0 or errors != 0:
        print(f'required tier failed: {summary}', file=sys.stderr)
        return 1
    print(f'required tier: {summary}')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv[1:]))
