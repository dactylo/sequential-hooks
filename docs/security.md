# Security

All three hosts start pre-tool command hooks before deciding whether to permit the proposed tool
call. That permission applies to the proposed tool, not to the hook process.

The wrapper and every step therefore:

- run with your user authority before the proposed tool is permitted;
- may read or write files, execute programs, and access the network;
- receive the host's inherited working directory and environment; and
- must be trusted before registration.

sequential-hooks does not sandbox, reduce privileges, inspect step intent, or prompt before
starting a step. Direct argument execution prevents shell reinterpretation inside the wrapper; it
does not make an untrusted command safe. A host-level command string still follows that host's
shell and quoting rules.

Process containment is hygiene, not a security boundary. It cleans up ordinary descendants during
timeouts, output failures, interrupts, and normal wrapper exit. It cannot recall work sent to an
existing daemon or service. POSIX descendants can leave the managed group. Windows loader activity
can precede the gated launcher's first Python statement.

`--allow-on-failure` may run a step without containment after a containment setup failure. That
step loses process-tree cleanup guarantees. Use the flag only after accepting the failure modes in
[platforms](platforms.md#containment-failure-override).

Post-tool feedback cannot undo an irreversible side effect. A block may stop later steps or inform
the host, but the completed file write, command, or network request has already happened.

Antigravity requires a final pre-tool decision. When every step is neutral, the wrapper emits
`allow`, which grants real automatic permission. Include a step returning `ask` or `force_ask` when
a prompt is required, or do not register that chain.

The wrapper cannot protect a chain from the failure of its own host adapter. When selection or
construction fails because the adapter is missing, duplicated, outdated, or malfunctioning, the
wrapper runs no step and exits 2 with one diagnostic line and no response. A runtime adapter
malfunction stops further processing but cannot undo children that already ran; it has the same
exit and output behavior. Claude Code and Codex treat that exit as blocking; Antigravity does not,
so its last resort grants nothing but also blocks nothing. Run `doctor` after installing or
upgrading an adapter package.

The wrapper keeps native input and derived state in memory for the invocation. It does not persist
stdin, stdout, stderr, or protocol state. `doctor` redacts secret-looking command values and never
prints full configuration files.
