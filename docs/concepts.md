# Concepts

## One active step

A host registration is a hook. Each child command inside that hook is a step. The wrapper starts
one step at a time and waits for its managed process tree to finish before it starts the next. A
terminal result stops the chain.

The first step receives the original host payload. Later steps receive the current native payload,
including any accepted replacement from an earlier step. They do not receive earlier stdout or
wrapper-specific metadata.

## Replacements, not patches

Supported mutations replace one native field:

- Claude Code pre-tool `updatedInput` replaces `tool_input`.
- Claude Code post-tool `updatedToolOutput` replaces `tool_response`.
- Codex pre-tool `updatedInput` replaces `tool_input`.

A later valid replacement supersedes an earlier one. Codex post-tool and both Antigravity events
have no documented mutation.

## Monotonic decisions

A later step may strengthen a safety decision but cannot weaken it. Claude Code supplies its own
pre-tool ordering: `deny > defer > ask > allow`. The Codex and Antigravity orderings are wrapper
contracts because those hosts document meanings but no ordering for disagreeing hooks.

Decision-bearing mutations remain attached to a compatible final decision. Size reduction never
drops a mutation and then permits the original operation.

## Explicit selection

`--agent` names an installed adapter; the wrapper infers nothing from the payload shape. Adapters
are discovered through the `sequential_hooks.hosts` entry-point group, and the built-in hosts are
registered there like any other. A host whose payload carries no event name requires `--event`; a
host whose payload carries the event rejects it. See [adding a host](adding-a-host.md).

## Failure tiers

A child failure reaches a functioning adapter, which answers with its native fail-closed response.
A configuration or validation error with a loaded host and a trusted event is answered by that
host's own configuration response, computed from the host and the event alone. When no functioning
host exists, the wrapper writes nothing to stdout, one diagnostic line to stderr, and exits 2. That
last resort blocks on Claude Code and Codex and does not block on Antigravity, which has no exit
code tier; each host guide states its boundary.

## Host-owned classification

Each host decides what stdout and exit codes mean. Claude Code may treat successful plain text as
native output. Codex distinguishes JSON-looking malformed text from neutral text. Antigravity
requires event-specific JSON. The common runner never forces one classification rule across the
three hosts.

## Diagnostic tiers

Developer diagnostics go to wrapper stderr. They include labeled step stderr, lifecycle failures,
and containment state. User diagnostics use a native host response field only when the chain failed
and the user must act.

Antigravity has no independent user-message field. A warning can accompany an existing native
decision reason, but the wrapper does not invent a permission decision merely to display a message.
Run `doctor` when setting up or diagnosing that host.

See [host protocols](reference/host-protocols.md) and [troubleshooting](troubleshooting.md).
