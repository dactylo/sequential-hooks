# Platforms

sequential-hooks requires Python 3.13 or newer and a native CPython interpreter for the current
platform. WSL is Linux. Native Windows CPython launched from Git Bash is Windows. MSYS2 and Cygwin
interpreters are incompatible because their process and signal emulation matches neither native
contract.

Host compatibility and platform compatibility are separate. See [compatibility](compatibility.md)
for supported hosts and known limits. This page describes platform process handling; verify the
host and platform combination you intend to use.

## Common process contract

For every termination path the wrapper controls:

- A timeout, output limit, or forwarded interrupt terminates the active managed tree.
- No managed descendant survives a normal wrapper exit or handled signal.
- Except for the narrow POSIX identifier-reuse window below, the wrapper signals only the process
  tree it started.
- A descendant holding an inherited pipe cannot stall completion forever.
- Cleanup does not corrupt protocol stdout.

Each step receives the inherited working directory and environment. It runs as a literal argument
vector without a shell.

## POSIX

On macOS and Linux, each step starts a new session. Cleanup signals its original process group,
waits a bounded grace period, then escalates. Liveness probes address that exact group.

Four exclusions matter:

- An uncatchable wrapper death can leave descendants running because POSIX does not kill children
  when their parent dies.
- A descendant that calls `setsid` or moves with `setpgid` leaves the original group.
- Work delegated to an existing service, scheduler, daemon, or broker is outside the process tree.
- The extremely narrow reuse window begins only after the original process group is empty and its
  leader is reaped. POSIX may then reuse the leader's PID as a new process group's PGID before
  cleanup sends its final signal, so a newly reused identifier could receive the final
  process-group signal. Avoiding the window here would weaken descendant cleanup or require
  disproportionate machinery.

The wrapper forwards `SIGINT`, `SIGTERM`, and `SIGHUP` to the active group, cleans up, and starts
no later step.

## Native Windows

The targeted minimum is Windows 10. A gated launcher blocks before it creates the real step. The
wrapper creates a kill-on-close Job Object with no breakaway, starts the launcher with the native
interpreter in isolated no-site mode, assigns it to the job, and only then releases it. Descendants
join the job automatically.

The operating-system loader runs before the launcher's first Python statement. Code started during
loader initialization may escape. Brokered creation and work delegated to an existing service are
also outside the job.

Closing the wrapper's only job handle terminates remaining members, including after forced wrapper
termination. The launcher relays the step's full unsigned 32-bit exit value.

The wrapper classifies a value as a crash only when it falls in `0xC0000000..0xCFFFFFFF`, falls in
`0xE0000000..0xEFFFFFFF`, or equals one of `0x80000001`, `0x80000002`, `0x80000003`, and
`0x80000004`. Values surrounding the four exact `0x8000000x` entries are ordinary exits. An
intentional exit inside either range or the exact set collides with the crash predicate and fails
closed by default. On a pre-tool event, `--allow-on-failure` reports that crash and continues;
post-tool already reports and continues.

A graceful console interrupt requires a shared console. The wrapper handles `SIGINT` and
`SIGBREAK`; when no console exists or the grace period expires, it terminates the job. Forced
termination remains available in both console and no-console launches.

The launcher reconstructs a C-runtime argument string from the literal step vector. This cannot
promise another program's private parser semantics. A step must resolve to a real executable image.
Windows `.bat`, `.cmd`, and `.ps1` shims are rejected. Use a real `.exe` or `.com` entry point; do
not add a shell wrapper.

## Containment failure override

The default refuses to run a step when containment cannot be established. `--allow-on-failure`
relaxes only listed infrastructure failures:

- If Job Object creation fails before a launcher exists, the step starts directly and uncontained.
- If launcher creation fails after Job Object creation, the wrapper closes the job and starts the
  step directly and uncontained.
- If assignment fails while the gated launcher waits, the wrapper releases it uncontained.
- If control delivery fails, the wrapper disposes of the unknown launcher and starts the step
  directly.

An uncontained step loses descendant cleanup, stalled-pipe protection, and controlled tree
termination guarantees. The first wrapper termination of that step produces a user-visible native
message for Claude Code and Codex. Antigravity can attach the warning only to a winning `ask`,
`force_ask`, or `deny` reason. An all-neutral or winning `allow` result leaves the warning in
developer stderr only. The flag never weakens a native decision, accepts invalid input, relaxes an
undeliverable decision-bearing mutation, overrides an interrupt, or recovers an internal invariant
failure.
