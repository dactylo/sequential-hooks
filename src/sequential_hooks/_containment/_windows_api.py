"""Wrap the Windows APIs required for process containment and completion."""

from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TypedDict

from sequential_hooks._cleanup import CleanupState

if TYPE_CHECKING:
    import subprocess

_ASSIGN_PROCESS_RIGHTS = 0x0001 | 0x0100
_CTRL_BREAK_EVENT = 1
_DO_NOT_INHERIT_HANDLE = False
_ERROR_INVALID_PARAMETER = 87
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_CLASS = 7
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_MSG_NEW_PROCESS = 6
_JOB_POLL_SECONDS = 0.01
_QUERY_AND_SYNCHRONIZE_PROCESS_RIGHTS = 0x1000 | 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258


class _JobCompletion(TypedDict):
    """Retain creation notifications and native completion owners for one job."""

    _completed: bool
    _observed_processes: int
    _port_handle: int
    _process_handles: list[int]


class _IoCounters(ctypes.Structure):
    """Mirror the Win32 `IO_COUNTERS` layout."""

    _fields_ = [
        ('ReadOperationCount', ctypes.c_ulonglong),
        ('WriteOperationCount', ctypes.c_ulonglong),
        ('OtherOperationCount', ctypes.c_ulonglong),
        ('ReadTransferCount', ctypes.c_ulonglong),
        ('WriteTransferCount', ctypes.c_ulonglong),
        ('OtherTransferCount', ctypes.c_ulonglong),
    ]


class _JobObjectAssociateCompletionPort(ctypes.Structure):
    """Mirror the Win32 `JOBOBJECT_ASSOCIATE_COMPLETION_PORT` layout."""

    _fields_ = [
        ('CompletionKey', ctypes.c_void_p),
        ('CompletionPort', wintypes.HANDLE),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    """Mirror the Win32 `JOBOBJECT_BASIC_ACCOUNTING_INFORMATION` layout."""

    _fields_ = [
        ('TotalUserTime', ctypes.c_longlong),
        ('TotalKernelTime', ctypes.c_longlong),
        ('ThisPeriodTotalUserTime', ctypes.c_longlong),
        ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
        ('TotalPageFaultCount', wintypes.DWORD),
        ('TotalProcesses', wintypes.DWORD),
        ('ActiveProcesses', wintypes.DWORD),
        ('TotalTerminatedProcesses', wintypes.DWORD),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    """Mirror the Win32 `JOBOBJECT_BASIC_LIMIT_INFORMATION` layout."""

    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    """Mirror the Win32 `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` layout."""

    _fields_ = [
        ('BasicLimitInformation', _JobObjectBasicLimitInformation),
        ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class NativeWindowsApi:
    """Call the kernel32 surface required for containment and process completion.

    Callers own handles returned by `create_kill_on_close_job()` until passing
    each handle to `close_handle()`. `assign_pid()` owns and closes only its
    temporary process handle.
    """

    def __init__(self) -> None:
        """Bind the required `kernel32` functions and their ctypes signatures.

        Raises:
            OSError: If native Windows APIs cannot be loaded.
        """
        win_dll = getattr(ctypes, 'WinDLL', None)
        if win_dll is None:
            raise OSError('native Windows APIs are unavailable')
        self._jobs: dict[int, _JobCompletion] = {}
        self._kernel32: Any = win_dll('kernel32', use_last_error=True)
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateIoCompletionPort.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        self._kernel32.CreateIoCompletionPort.restype = wintypes.HANDLE
        self._kernel32.CreateJobObjectW.argtypes = [
            wintypes.LPVOID,
            wintypes.LPCWSTR,
        ]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.GenerateConsoleCtrlEvent.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        self._kernel32.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.GetQueuedCompletionStatus.restype = wintypes.BOOL
        self._kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.LPVOID,
        ]
        self._kernel32.IsProcessInJob.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD

    @staticmethod
    def _raise_last_error(operation: str) -> NoReturn:
        """Raise one bounded operating-system error for a failed Win32 call."""
        get_last_error = getattr(ctypes, 'get_last_error', lambda: 0)
        error_code = int(get_last_error())
        raise OSError(error_code, f'{operation} failed with Windows error {error_code}')

    def _close_native_handle(self, handle: int) -> None:
        """Close one kernel handle without dispatching job ownership cleanup."""
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error('CloseHandle')

    def _capture_job_process(self, job_handle: int, job: _JobCompletion, pid: int) -> None:
        """Retain each notified process still belonging to its original job."""
        process_handle = self._kernel32.OpenProcess(
            _QUERY_AND_SYNCHRONIZE_PROCESS_RIGHTS,
            _DO_NOT_INHERIT_HANDLE,
            pid,
        )
        if not process_handle:
            if getattr(ctypes, 'get_last_error', lambda: 0)() == _ERROR_INVALID_PARAMETER:
                # A destroyed process cannot still have pending native teardown.
                job['_observed_processes'] += 1
                return
            self._raise_last_error('OpenProcess')
        handle = int(process_handle)
        retained = False
        cleanup = CleanupState()
        try:
            with cleanup:
                in_job = wintypes.BOOL()
                if not self._kernel32.IsProcessInJob(handle, job_handle, ctypes.byref(in_job)):
                    self._raise_last_error('IsProcessInJob')
                if in_job.value:
                    job['_process_handles'].append(handle)
                    retained = True
                # A reused PID in another job means the original is already gone.
                job['_observed_processes'] += 1
        finally:
            if not retained:
                cleanup.attempt(lambda: self._close_native_handle(handle))
        if cleanup.first_error is not None:
            raise cleanup.first_error

    def _receive_job_message(
        self,
        job_handle: int,
        job: _JobCompletion,
        timeout_seconds: float,
    ) -> None:
        """Consume one queued creation event or a bounded notification wait."""
        message = wintypes.DWORD()
        key = ctypes.c_size_t()
        pid = ctypes.c_void_p()
        if not self._kernel32.GetQueuedCompletionStatus(
            job['_port_handle'],
            ctypes.byref(message),
            ctypes.byref(key),
            ctypes.byref(pid),
            max(0, math.ceil(timeout_seconds * 1000)),
        ):
            if getattr(ctypes, 'get_last_error', lambda: 0)() == _WAIT_TIMEOUT:
                return
            self._raise_last_error('GetQueuedCompletionStatus')
        if key.value != job_handle:
            raise OSError('unexpected Windows job completion key')
        if message.value == _JOB_OBJECT_MSG_NEW_PROCESS:
            if pid.value is None:
                raise OSError('Windows job creation notification omits process identity')
            self._capture_job_process(job_handle, job, pid.value)

    def close_handle(self, handle: int) -> None:
        """Close one owned kernel handle and any associated job resources.

        Args:
            handle: Owned nonzero kernel handle.

        Raises:
            OSError: If a native close fails; all owners are still attempted.
        """
        cleanup = CleanupState()
        if job := self._jobs.pop(handle, None):
            for process_handle in job['_process_handles']:
                cleanup.attempt(lambda handle=process_handle: self._close_native_handle(handle))
            cleanup.attempt(lambda: self._close_native_handle(job['_port_handle']))
        cleanup.attempt(lambda: self._close_native_handle(handle))
        if cleanup.first_error is not None:
            raise cleanup.first_error

    def assign_pid(self, job_handle: int, pid: int) -> None:
        """Assign one live process and close the temporary process handle.

        Args:
            job_handle: Owned Job Object handle.
            pid: Live launcher process identifier.

        Raises:
            OSError: If opening, assigning, or closing the process handle
                fails.
        """
        process_handle = self._kernel32.OpenProcess(
            _ASSIGN_PROCESS_RIGHTS,
            _DO_NOT_INHERIT_HANDLE,
            pid,
        )
        if not process_handle:
            self._raise_last_error('OpenProcess')
        try:
            if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
                self._raise_last_error('AssignProcessToJobObject')
        finally:
            self.close_handle(int(process_handle))

    def create_kill_on_close_job(self) -> int:
        """Create and configure a Job Object with kill-on-close.

        Returns:
            Owned Job Object handle with no breakaway permission.

        Raises:
            OSError: If job creation, configuration, or cleanup fails.
        """
        job_handle = self._kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            self._raise_last_error('CreateJobObjectW')
        handle = int(job_handle)
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        cleanup = CleanupState()
        port_handle = 0
        with cleanup:
            if not self._kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                self._raise_last_error('SetInformationJobObject')
            port = self._kernel32.CreateIoCompletionPort(_INVALID_HANDLE_VALUE, None, 0, 1)
            if not port:
                self._raise_last_error('CreateIoCompletionPort')
            port_handle = int(port)
            association = _JobObjectAssociateCompletionPort(handle, port_handle)
            if not self._kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_CLASS,
                ctypes.byref(association),
                ctypes.sizeof(association),
            ):
                self._raise_last_error('SetInformationJobObject')
        if cleanup.first_error is not None:
            if port_handle:
                cleanup.attempt(lambda: self._close_native_handle(port_handle))
            cleanup.attempt(lambda: self._close_native_handle(handle))
            raise cleanup.first_error
        self._jobs[handle] = {
            '_completed': False,
            '_observed_processes': 0,
            '_port_handle': port_handle,
            '_process_handles': [],
        }
        return handle

    def generate_ctrl_break(self, process_group_id: int) -> bool:
        """Try to send Ctrl+Break to one console process group.

        Args:
            process_group_id: Launcher process-group identifier.

        Returns:
            Whether the console accepted the event.
        """
        return bool(self._kernel32.GenerateConsoleCtrlEvent(_CTRL_BREAK_EVENT, process_group_id))

    def terminate_job(self, job_handle: int) -> None:
        """Terminate every process currently assigned to one job.

        Args:
            job_handle: Owned Job Object handle.

        Raises:
            OSError: If `TerminateJobObject` fails.
        """
        if not self._kernel32.TerminateJobObject(job_handle, 1):
            self._raise_last_error('TerminateJobObject')

    def wait_job(self, job_handle: int, timeout_seconds: float) -> bool:
        """Confirm complete creation tracking and native teardown of every member.

        Args:
            job_handle: Owned Job Object handle retained throughout the wait.
            timeout_seconds: Nonnegative maximum wait in seconds.

        Returns:
            Whether every job member has completed native teardown. Missing
            creation notifications or an exhausted deadline return `False`.

        Raises:
            OSError: If querying, tracking, or waiting for job members fails.
        """
        job = self._jobs[job_handle]
        if job['_completed']:
            return True
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        information = _JobObjectBasicAccountingInformation()
        while True:
            if not self._kernel32.QueryInformationJobObject(
                job_handle,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
                None,
            ):
                self._raise_last_error('QueryInformationJobObject')
            # Native probes observe zero accounting and exit codes before process
            # handles signal. Count events to detect missing notifications, then wait
            # on retained identities even after they disappear from job lists.
            if (
                information.ActiveProcesses == 0
                and job['_observed_processes'] == information.TotalProcesses
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._receive_job_message(job_handle, job, min(_JOB_POLL_SECONDS, remaining))
        for process_handle in job['_process_handles']:
            remaining = max(0.0, deadline - time.monotonic())
            status = int(self._kernel32.WaitForSingleObject(process_handle, int(remaining * 1000)))
            if status == _WAIT_TIMEOUT:
                return False
            if status != _WAIT_OBJECT_0:
                self._raise_last_error('WaitForSingleObject')
        job['_completed'] = True
        return True

    def wait_process(self, process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        """Wait for native completion independently of Popen's cached status.

        Args:
            process: Live owning CPython Popen object; its handle is borrowed,
                never reopened or closed here.
            timeout_seconds: Nonnegative maximum wait in seconds.

        Returns:
            Whether the retained process handle became signaled.

        Raises:
            OSError: If the retained handle is unavailable or the native wait
                fails.
        """
        # CPython exposes no public Windows handle. Keep this implementation
        # dependency at one validated platform boundary; Popen owns its lifetime.
        handle = vars(process).get('_handle')
        if not isinstance(handle, int) or handle <= 0:
            raise OSError('owned Windows process handle is unavailable')
        milliseconds = max(0, int(timeout_seconds * 1000))
        status = int(self._kernel32.WaitForSingleObject(handle, milliseconds))
        if status not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
            self._raise_last_error('WaitForSingleObject')
        return status == _WAIT_OBJECT_0


class WindowsApi(Protocol):
    """Provide the Job Object, console, and process waits used by containment.

    Callers own each handle returned by `create_kill_on_close_job()` until
    passing it to `close_handle()`. Operations raise `OSError` on native
    failure except `generate_ctrl_break()`, which reports acceptance as a
    Boolean result.
    """

    def assign_pid(self, job_handle: int, pid: int) -> None:
        """Open the live process, assign it, and close that process handle.

        Args:
            job_handle: Job Object handle that receives the process.
            pid: Live process identifier to assign.

        Raises:
            OSError: If opening, assigning, or closing the process handle
                fails.
        """
        ...

    def close_handle(self, handle: int) -> None:
        """Close one owned kernel handle exactly once.

        Args:
            handle: Owned kernel handle to close.

        Raises:
            OSError: If `CloseHandle` fails.
        """
        ...

    def create_kill_on_close_job(self) -> int:
        """Return a Job Object handle with no breakaway permission.

        Returns:
            Owned kill-on-close Job Object handle.

        Raises:
            OSError: If job creation, configuration, or cleanup fails.
        """
        ...

    def generate_ctrl_break(self, process_group_id: int) -> bool:
        """Try to deliver Ctrl+Break to one console process group.

        Args:
            process_group_id: Console process-group identifier.

        Returns:
            Whether the console accepted the event.
        """
        ...

    def terminate_job(self, job_handle: int) -> None:
        """Terminate every process in the job.

        Args:
            job_handle: Job Object handle whose processes must terminate.

        Raises:
            OSError: If `TerminateJobObject` fails.
        """
        ...

    def wait_job(self, job_handle: int, timeout_seconds: float) -> bool:
        """Wait for all members of a retained job to exit.

        Args:
            job_handle: Owned Job Object handle retained throughout the wait.
            timeout_seconds: Nonnegative maximum wait in seconds.

        Returns:
            Whether all job members, including nested-job members, have exited.

        Raises:
            OSError: If querying job accounting fails.
        """
        ...

    def wait_process(self, process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
        """Wait on the retained handle without trusting cached exit status.

        Args:
            process: Popen that retains ownership of the immediate process.
            timeout_seconds: Nonnegative maximum wait in seconds.

        Returns:
            Whether the process has completed native teardown.

        Raises:
            OSError: If the owned handle is unavailable or the native wait
                fails.
        """
        ...


def probe_job_object(api: WindowsApi | None = None) -> bool:
    """Test whether a kill-on-close Job Object can be configured.

    Args:
        api: Windows API boundary to probe, or the native implementation.

    Returns:
        `True` when creation, configuration, and handle cleanup succeed;
        `False` when an operation raises `OSError`.
    """
    try:
        selected_api = api or NativeWindowsApi()
        handle = selected_api.create_kill_on_close_job()
        selected_api.close_handle(handle)
    except OSError:
        return False
    return True
