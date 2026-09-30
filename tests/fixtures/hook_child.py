import ctypes
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast


def _echo_input(_args: list[str], data: bytes) -> int:
    sys.stdout.buffer.write(data)
    return 0


def _emit_hex(args: list[str], _data: bytes) -> int:
    sys.stdout.buffer.write(bytes.fromhex(args[0]))
    return 0


def _exit(args: list[str], _data: bytes) -> int:
    if len(args) > 1:
        sys.stderr.buffer.write(f'{args[1]}\n'.encode())
    return int(args[0])


def _gated_side_effect(args: list[str], data: bytes) -> int:
    gate_file = Path(args[0])
    while not gate_file.exists():
        time.sleep(0.005)
    with Path(args[1]).open('a', encoding='utf-8') as stream:
        stream.write(f'{args[2]}\n')
    sys.stdout.buffer.write(data)
    return 0


def _invalid_stderr(_args: list[str], _data: bytes) -> int:
    os.write(sys.stderr.fileno(), b'bad:\xff\n')
    return 0


def _publish(path_value: str, content: str) -> None:
    Path(path_value).write_text(content, encoding='utf-8')
    Path(f'{path_value}.published').touch()


def _record_child_tree(args: list[str]) -> None:
    ready_file = Path(f'{args[1]}.ready')
    child = subprocess.Popen(
        [sys.executable, __file__, 'sleep-ignore-term', args[0], str(ready_file)]
    )
    while not Path(f'{ready_file}.published').exists():
        time.sleep(0.005)
    _publish(
        args[1],
        json.dumps({'parent': os.getpid(), 'child': child.pid}),
    )


def _child_tree(args: list[str], _data: bytes) -> int:
    _record_child_tree(args)
    time.sleep(float(args[0]))
    return 0


def _child_tree_exit(args: list[str], _data: bytes) -> int:
    _record_child_tree(args)
    return 0


def _child_tree_output(args: list[str], _data: bytes) -> int:
    _record_child_tree(args)
    sys.stdout.buffer.write(b'x' * int(args[2]))
    sys.stdout.buffer.flush()
    time.sleep(float(args[0]))
    return 0


def _record_escaped_process(args: list[str], mode: str) -> None:
    ready_file = Path(f'{args[1]}.ready')
    child = subprocess.Popen(
        [sys.executable, __file__, mode, args[0], str(ready_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    while not Path(f'{ready_file}.published').exists():
        time.sleep(0.005)
    _publish(
        args[1],
        json.dumps({'parent': os.getpid(), 'escaped': child.pid}),
    )


def _child_tree_setpgid(args: list[str], _data: bytes) -> int:
    _record_escaped_process(args, 'setpgid-sleep')
    time.sleep(float(args[0]))
    return 0


def _child_tree_setsid(args: list[str], _data: bytes) -> int:
    _record_escaped_process(args, 'setsid-sleep')
    time.sleep(float(args[0]))
    return 0


def _record_side_effect(args: list[str]) -> int:
    with Path(args[0]).open('a', encoding='utf-8') as stream:
        stream.write(f'{args[1]}\n')
    sys.stdout.buffer.write(sys.stdin.buffer.read())
    return 0


def _record_signal(args: list[str], _data: bytes) -> int:
    signal_file = Path(args[0])
    ready_file = Path(args[1])

    def handle_signal(signum: int, _frame: object) -> None:
        signal_file.write_text(
            json.dumps({'pid': os.getpid(), 'signum': signum}),
            encoding='utf-8',
        )
        raise SystemExit(0)

    for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'):
        if (signum := getattr(signal, name, None)) is not None:
            signal.signal(signum, handle_signal)
    ready_pending = ready_file.with_name(f'{ready_file.name}.pending')
    ready_pending.write_text(str(os.getpid()), encoding='utf-8')
    ready_pending.replace(ready_file)
    deadline = time.monotonic() + float(args[2])
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(0.01, remaining))
    return 0


def _report_process(_args: list[str], _data: bytes) -> int:
    if sys.platform == 'win32':
        raise OSError('POSIX fixture mode is unavailable on Windows')
    print(
        json.dumps(
            {
                'pid': os.getpid(),
                'pgid': os.getpgid(0),
                'sid': os.getsid(0),
            }
        )
    )
    return 0


def _self_signal(args: list[str], _data: bytes) -> int:
    os.kill(os.getpid(), int(args[0]))
    return 0


def _setpgid_sleep(args: list[str], _data: bytes) -> int:
    if sys.platform == 'win32':
        raise OSError('POSIX fixture mode is unavailable on Windows')
    os.setpgid(0, 0)
    _publish(args[1], '')
    time.sleep(float(args[0]))
    return 0


def _setsid_sleep(args: list[str], _data: bytes) -> int:
    if sys.platform == 'win32':
        raise OSError('POSIX fixture mode is unavailable on Windows')
    os.setsid()
    _publish(args[1], '')
    time.sleep(float(args[0]))
    return 0


def _show_argv(args: list[str], _data: bytes) -> int:
    sys.stdout.buffer.write(json.dumps(args).encode('utf-8') + b'\n')
    return 0


def _sleep(args: list[str], _data: bytes) -> int:
    time.sleep(float(args[0]))
    return 0


def _sleep_ignore_term(args: list[str], _data: bytes) -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _publish(args[1], str(os.getpid()))
    time.sleep(float(args[0]))
    return 0


def _stderr(args: list[str], _data: bytes) -> int:
    sys.stderr.buffer.write(f'{args[0]}\n'.encode())
    return 0


def _stderr_bytes(args: list[str], _data: bytes) -> int:
    sys.stderr.buffer.write(b'x' * int(args[0]))
    return 0


def _stdout_bytes(args: list[str], _data: bytes) -> int:
    sys.stdout.buffer.write(b'x' * int(args[0]))
    return 0


def _stdout_bytes_then_sleep(args: list[str], data: bytes) -> int:
    status = _stdout_bytes(args, data)
    sys.stdout.buffer.flush()
    time.sleep(float(args[1]))
    return status


def _validate_argv(args: list[str], _data: bytes) -> int:
    expected = cast('object', json.loads(args[0]))
    if args[2:] != expected:
        raise ValueError(f'argv has unexpected value: {args[2:]!r}')
    sys.stdout.buffer.write(bytes.fromhex(args[1]))
    return 0


def _validate_json_field(args: list[str], data: bytes) -> int:
    value = cast('object', json.loads(data))
    for component in args[0].split('.'):
        if not isinstance(value, dict):
            raise TypeError(f'{component} parent is not an object')
        mapping = cast('dict[str, object]', value)
        value = mapping[component]
    if value != json.loads(args[1]):
        raise ValueError(f'field {args[0]} has unexpected value: {value!r}')
    sys.stdout.buffer.write(bytes.fromhex(args[2]))
    return 0


def _windows_exit(args: list[str]) -> None:
    win_dll = vars(ctypes)['WinDLL']
    kernel32 = win_dll('kernel32', use_last_error=True)
    kernel32.ExitProcess.argtypes = [ctypes.c_uint32]
    kernel32.ExitProcess.restype = None
    kernel32.ExitProcess(ctypes.c_uint32(int(args[0], 0)).value)


def _windows_force_wrapper_exit(args: list[str]) -> None:
    backend_type = vars(importlib.import_module('sequential_hooks._containment._windows'))[
        'WindowsBackend'
    ]
    step_type = vars(importlib.import_module('sequential_hooks._types'))['Step']

    record_file = Path(args[0])
    managed = backend_type().start(
        step_type(
            1,
            (
                sys.executable,
                __file__,
                'child-tree',
                '10',
                str(record_file),
            ),
        ),
        allow_uncontained=False,
    )
    managed.release()
    managed.stdin.close()
    deadline = time.monotonic() + 3.0
    while not Path(f'{record_file}.published').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('managed child tree did not publish readiness')
        time.sleep(0.005)
    _windows_exit(['0xC0000005'])


def _windows_no_console_cleanup(args: list[str]) -> None:
    kernel32 = vars(ctypes)['WinDLL']('kernel32', use_last_error=True)
    kernel32.FreeConsole.argtypes = []
    kernel32.FreeConsole.restype = ctypes.c_int
    kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32]
    kernel32.GetConsoleProcessList.restype = ctypes.c_uint32
    if not kernel32.FreeConsole():
        raise OSError('FreeConsole failed')
    process_ids = (ctypes.c_uint32 * 1)()
    console_process_count = kernel32.GetConsoleProcessList(process_ids, 1)
    if console_process_count != 0 or vars(ctypes)['get_last_error']() != 6:
        raise AssertionError('fixture still has a console after FreeConsole')
    windows_module = importlib.import_module('sequential_hooks._containment._windows')
    api_module = importlib.import_module('sequential_hooks._containment._windows_api')
    types_module = importlib.import_module('sequential_hooks._types')
    backend_type = vars(windows_module)['WindowsBackend']
    api_type = vars(api_module)['NativeWindowsApi']
    step_type = vars(types_module)['Step']

    record_file = Path(args[0])
    outcome_file = Path(args[1])
    api = api_type()
    managed = backend_type(api).start(
        step_type(
            1,
            (
                sys.executable,
                __file__,
                'child-tree',
                '10',
                str(record_file),
            ),
        ),
        allow_uncontained=False,
    )
    managed.release()
    managed.stdin.close()
    deadline = time.monotonic() + 3.0
    while not Path(f'{record_file}.published').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('no-console child tree did not publish readiness')
        time.sleep(0.005)
    delivered = api.generate_ctrl_break(managed._raw_process.pid)
    managed.terminate_tree(0.0)
    managed.finish()
    for stream in (managed.stdout, managed.stderr):
        stream.close()
    outcome_file.write_text(
        json.dumps(
            {
                'console_process_count': console_process_count,
                'ctrl_break_delivered': delivered,
                'pids': json.loads(record_file.read_text(encoding='utf-8')),
            }
        ),
        encoding='utf-8',
    )


def _write_marker(args: list[str], _data: bytes) -> int:
    Path(args[0]).write_text(args[1], encoding='utf-8')
    return 0


def main() -> int:
    mode, *args = sys.argv[1:]
    if mode == 'exit-without-stdin':
        return 0
    if mode == 'record-side-effect':
        return _record_side_effect(args)
    if mode == 'windows-exit':
        _windows_exit(args)
        raise AssertionError('ExitProcess returned')
    if mode == 'windows-force-wrapper-exit':
        _windows_force_wrapper_exit(args)
        raise AssertionError('ExitProcess returned')
    if mode == 'windows-no-console-cleanup':
        _windows_no_console_cleanup(args)
        return 0
    handlers = {
        'child-tree': _child_tree,
        'child-tree-exit': _child_tree_exit,
        'child-tree-output': _child_tree_output,
        'child-tree-setpgid': _child_tree_setpgid,
        'child-tree-setsid': _child_tree_setsid,
        'echo-input': _echo_input,
        'emit-hex': _emit_hex,
        'exit': _exit,
        'gated-side-effect': _gated_side_effect,
        'invalid-stderr': _invalid_stderr,
        'record-signal': _record_signal,
        'report-process': _report_process,
        'self-signal': _self_signal,
        'setpgid-sleep': _setpgid_sleep,
        'setsid-sleep': _setsid_sleep,
        'show-argv': _show_argv,
        'sleep': _sleep,
        'sleep-ignore-term': _sleep_ignore_term,
        'stderr': _stderr,
        'stderr-bytes': _stderr_bytes,
        'stdout-bytes': _stdout_bytes,
        'stdout-bytes-then-sleep': _stdout_bytes_then_sleep,
        'validate-argv': _validate_argv,
        'validate-json-field': _validate_json_field,
        'write-marker': _write_marker,
    }
    try:
        handler = handlers[mode]
    except KeyError as error:
        raise ValueError(f'unknown fixture mode: {mode}') from error
    return handler(args, sys.stdin.buffer.read())


if __name__ == '__main__':
    raise SystemExit(main())
