# Brokered runtime

The repository broker is a capability-based reference monitor. The broker runs outside the worker sandbox, owns the repository mount and signing keys, validates the live admitted intent on every request, and records a durable operation before any filesystem mutation.

## Trust model

The worker receives only a Unix socket and bearer token. The broker owns:

- the repository root and exact Git base commit;
- the current admitted intent and its content version;
- a distinct observation key and broker-attestation key;
- the SQLite control-plane state;
- the operation recovery journal;
- the allowlisted command policy.

A successful request follows this sequence:

```text
authenticate
→ validate live intent, lease, version, base and repository identity
→ check exact operation capability
→ write pending broker operation
→ perform repository action
→ append broker-bound observation event
→ commit signed operation receipt
```

If observation commit fails after a mutation, the broker restores the previous content and records the operation as `rolled_back`. Pending operations are recovered on broker startup.

## Exact capabilities

Broker tools do not share a generic mutation permission:

| Broker operation | Required intent access |
|---|---|
| `read_file`, `list_dir`, `search_text`, `stat` | Any declared operation covering the path; writes imply read-before-write access. |
| `write_file` | `write`, `document`, or `test` |
| `append_file` | `extend` |
| `replace_lines` | `write`, `document`, or `test`; bounded regions are enforced |
| `delete_file` | `delete` |
| `rename_file` | `rename`, with `rename_to`, `target`, or `to` metadata matching the destination |
| `run_command` | At least one declared `test` operation and a named command in the broker allowlist |

A `write` capability cannot delete or rename. An `extend` capability cannot replace an existing file.

## Live revocation

The broker does not cache authority for the lifetime of the process. Every request verifies:

- broker-instance HMAC attestation;
- intent state is `admitted` or `active`;
- intent lease has not expired;
- intent `content_version` and fingerprint match the registered capability;
- exact `base_commit` remains unchanged;
- repository root and Git common-directory identity remain unchanged;
- observation session is still open and bound to the broker instance.

Release, expiration, stale invalidation, or amendment revokes the running broker before the next operation.

A successful contingent-scope promotion is the deliberate exception to ordinary content-
version revocation. The broker requests atomic promotion before the first mutation, and the
registry advances that broker's signed intent binding to the promoted content version in
the same transaction. A rejected promotion leaves both the intent and broker capability
unchanged, and the attempted mutation is denied.

## Broker instance and operation evidence

A broker instance is bound to:

- instance and session IDs;
- intent ID and content version;
- repository root identity;
- exact base commit;
- broker policy digest;
- broker module digest;
- monitor and key IDs.

Each operation has a signed prepare receipt and, when successful, a signed commit receipt linked to the exact observation-event range. Brokered integration verifies both the session chain and the broker operation journal. A manually created `brokered_proxy` session without a registered broker instance is rejected.

## Allowlisted commands

The broker does not expose arbitrary shell execution. A deployment may provide named commands:

```json
{
  "unit-tests": {
    "argv": ["python", "-m", "pytest", "-q"],
    "timeout_seconds": 300
  }
}
```

`run_command` freezes the current worktree into an immutable Git snapshot, materializes a detached worktree, executes the allowlisted command under the configured sandbox, and rejects commands that mutate the snapshot. The original broker root is never used as the command working directory.

## Linux proxy-only boundary

`claim-plane broker-run` builds a Bubblewrap namespace with no repository or host-home mount. The agent must use the broker socket for repository operations. Non-bypassability depends on deployment: an extra filesystem mount, privileged process, unrestricted remote connector, or second repository channel remains outside Claim Plane's visibility.

## macOS

The broker itself works on macOS. Bubblewrap does not. Run strict proxy-only workers in a Linux VM or container. `sandbox-exec` remains best-effort and is not equivalent to a Linux namespace.

macOS also has a shorter `AF_UNIX` path limit than Linux. Claim Plane automatically maps an overlong requested socket path—common under `~/Library/CloudStorage` and deeply nested pytest directories—to a deterministic per-user socket below `/tmp`. Broker clients and `broker-run` apply the same mapping, so callers can continue using the originally requested path. Set `CLAIM_PLANE_SOCKET_DIR` to choose another short private socket directory.

## Exclusive writer lease and tree provenance

A governed worktree can have only one active broker writer. Registration acquires an atomic SQLite lease keyed by the canonical worktree root. The lease is renewed on every validated request, released on clean shutdown, and expires abandoned instances fail-closed.

The broker also requires the worktree to be clean relative to the pinned base commit before it starts. Each mutating operation records and signs a Git-tree compare-and-swap transition:

```text
expected pre-tree
→ exact capability check
→ durable prepare
→ filesystem mutation
→ changed-path verification
→ signed post-tree commit
```

The control plane advances `expected_tree_hash` only when the prepared pre-tree still matches the current broker chain. Any direct edit outside the broker causes the next request to fail. During integration, the immutable frozen worker tree must equal the final broker-derived expected tree; otherwise the worker evidence is rejected before patches are applied.

## Local writer authority

Claim Plane combines three independent protections for one physical worktree:

```text
registry writer lease
+
OS-level worktree lock
+
monotonic fencing token
```

The registry lease coordinates brokers using the same state backend. The OS lock
lives under Git's canonical common directory and remains held by an open file
descriptor for the broker lifetime, so a second local Claim Plane process is
blocked even when it points at another SQLite database or reaches the repository
through a linked worktree path. Governed mode rejects alternate lock namespaces
from `--worktree-lock-dir` or `CLAIM_PLANE_LOCK_DIR`; those inputs are retained
only to fail explicitly during migration.

Broker byte mutations preserve the existing POSIX mode. New files are created as
`0644`; ordinary writes cannot silently clear an executable bit. Old and new
modes are included in the recovery journal and trusted observation metadata, and
rollback restores both bytes and mode.

Every new writer lease receives a larger fencing token. The token is bound into
the broker instance attestation, lease row, prepare record, observation metadata,
commit receipt, and verification path. A broker using an older token is rejected
before a mutation can be prepared or committed.

The OS lock is intentionally a single-host guarantee. Multi-host deployments
need a network-authoritative store and distributed lease, while retaining the
local OS lock on every worker host.
