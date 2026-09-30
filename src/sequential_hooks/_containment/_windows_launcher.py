"""Launch one gated Windows child and report its status."""

_release = __import__('os').read(
    (
        _control_fd := __import__('msvcrt').open_osfhandle(
            int(__import__('sys').argv[1]),
            __import__('os').O_RDONLY | __import__('os').O_BINARY,
        )
    ),
    1,
)
if _release != b'R':
    __import__('os').close(_control_fd)
    raise SystemExit(0)

_status_fd = __import__('msvcrt').open_osfhandle(
    int(__import__('sys').argv[2]),
    __import__('os').O_WRONLY | __import__('os').O_BINARY,
)
_os = __import__('os')
try:
    _contextlib = __import__('contextlib')
    _ctypes = __import__('ctypes')
    _json = __import__('json')
    _signal = __import__('signal')
    _subprocess = __import__('subprocess')
    _typing = __import__('typing')
except ImportError:
    _import_failure_message = b'launcher invariant failure'
    _import_failure_frame = (
        b'\x03' + len(_import_failure_message).to_bytes(4, 'little') + _import_failure_message
    )
    _import_failure_offset = 0
    while _import_failure_offset < len(_import_failure_frame):
        try:
            _import_failure_written = _os.write(
                _status_fd,
                _import_failure_frame[_import_failure_offset:],
            )
        except OSError:
            raise SystemExit(0) from None
        if _import_failure_written <= 0:
            raise SystemExit(0) from None
        _import_failure_offset += _import_failure_written
    for _descriptor in (_control_fd, _status_fd):
        try:
            _os.close(_descriptor)
        except OSError:
            continue
    raise SystemExit(0) from None
_ARGV_LIMIT_BYTES = 1024 * 1024
_STATUS_LIMIT_BYTES = 4096
_control_state = [_control_fd]
_status_state = [_status_fd]
_status_written = [False]


class _InvariantReporter:
    """Report and suppress post-release invariant exceptions as status 3."""

    def __enter__(self) -> None:
        """Begin guarded launcher execution."""

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: object,
    ) -> bool:
        """Report and suppress one launcher invariant exception."""
        if error is None:
            return False
        with _contextlib.suppress(BaseException):
            _write_status(3, 'launcher invariant failure')
        return True


def _close_control() -> None:
    """Close the control descriptor before creating the real child."""
    if _control_state[0] >= 0:
        _os.close(_control_state[0])
        _control_state[0] = -1


def _close_status() -> None:
    """Close the status descriptor exactly once."""
    if _status_state[0] >= 0:
        _os.close(_status_state[0])
        _status_state[0] = -1


def _ignore_break(_signum: int, _frame: object) -> None:
    """Keep the launcher alive while its real child handles Ctrl+Break."""


def _read_exact(count: int) -> bytes:
    """Read exactly one bounded control-frame component."""
    data = bytearray()
    while len(data) < count:
        chunk = _os.read(_control_state[0], count - len(data))
        if not chunk:
            raise ValueError('truncated launcher control frame')
        data.extend(chunk)
    return bytes(data)


def _write_all(data: bytes) -> None:
    """Write one exact status frame without reserving an exit value."""
    offset = 0
    while offset < len(data):
        written = _os.write(_status_state[0], data[offset:])
        if written <= 0:
            raise OSError('launcher status write made no progress')
        offset += written


def _write_status(status: int, message: str = '') -> None:
    """Write one bounded status frame with sanitized diagnostic text."""
    if _status_written[0]:
        return
    encoded = message.encode('utf-8')[: _STATUS_LIMIT_BYTES - 5]
    while True:
        try:
            encoded.decode('utf-8')
        except UnicodeDecodeError:
            encoded = encoded[:-1]
            continue
        break
    _write_all(bytes((status,)) + len(encoded).to_bytes(4, 'little') + encoded)
    _status_written[0] = True


def _main() -> None:
    """Validate the control frame, run one child, and relay exact status."""
    argv_length = int.from_bytes(_read_exact(8), 'little')
    if argv_length > _ARGV_LIMIT_BYTES:
        raise ValueError('launcher argv frame exceeds its bound')
    argv_value = _json.loads(_read_exact(argv_length).decode('utf-8'))
    if not isinstance(argv_value, list):
        raise TypeError('launcher argv must be a non-empty string list')
    argv_items = _typing.cast(list[object], argv_value)
    if not argv_items or any(not isinstance(value, str) for value in argv_items):
        raise ValueError('launcher argv must be a non-empty string list')
    argv = _typing.cast(list[str], argv_items)
    if _os.read(_control_state[0], 1):
        raise ValueError('launcher control frame contains trailing bytes')
    _close_control()
    break_signal = getattr(_signal, 'SIGBREAK', None)
    if break_signal is not None:
        _signal.signal(break_signal, _ignore_break)
    kernel32 = _typing.cast(
        _typing.Any,
        vars(_ctypes)['WinDLL']('kernel32', use_last_error=True),
    )
    exit_process = kernel32.ExitProcess
    exit_process.argtypes = [_ctypes.c_uint32]
    exit_process.restype = None
    try:
        child = _subprocess.Popen(
            argv,
            stdin=0,
            stdout=1,
            stderr=2,
            shell=False,
            close_fds=True,
        )
    except FileNotFoundError:
        _write_status(1, 'real step was not found')
        return
    except OSError:
        _write_status(2, 'real step could not be spawned')
        return
    except ValueError:
        _write_status(2, 'real step could not be spawned')
        return
    returncode = child.wait()
    try:
        _write_status(0)
        _close_status()
    finally:
        exit_process(_ctypes.c_uint32(returncode).value)


with _InvariantReporter():
    _main()
with _contextlib.suppress(OSError):
    _close_control()
with _contextlib.suppress(OSError):
    _close_status()
