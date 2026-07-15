# Storage backends and writer authority

Claim Plane separates the logical state-store contract from the local SQLite
implementation.

## Community / single-host mode

`SQLitePlaneStore` is the default and remains a supported permanent backend for:

- local development;
- one CI host;
- one machine running multiple agent worktrees;
- self-contained research experiments.

Two protections are combined:

```text
SQLite writer lease
+
OS-level worktree lock
+
monotonic fencing token
```

The SQLite lease coordinates brokers that share one database. The OS lock is derived from Git's canonical common directory and protects
the physical worktree even when two local processes use different SQLite files
or attempt to configure different lock directories. The fencing token prevents a superseded broker from completing a
request after a later writer has acquired the registry lease.

The OS lock is held by an open file descriptor for the broker lifetime. A normal
shutdown releases it explicitly; a crashed process releases it through operating
system descriptor cleanup.

## Multi-host / commercial control plane

A local lock cannot coordinate two machines. A distributed deployment requires a
single network-authoritative registry that preserves the `PlaneStore` semantics:

- atomic intent admission;
- distributed writer leases;
- monotonic fencing tokens;
- compare-and-swap tree transitions;
- dependency invalidation;
- append-only operation and evidence records.

PostgreSQL is the expected first network backend. It should not replace the local
OS lock: each worker host should retain the local lock while PostgreSQL owns the
distributed lease and fencing sequence.

```text
PostgreSQL authoritative lease
+
monotonic fencing token
+
local OS worktree lock
+
live validation before every mutation
```

## Backend API

`claim_plane.core.store.PlaneStore` is now the complete public structural boundary.
It is composed from narrower `ClaimStore`, `IntentStore`, `ObservationStore`,
`BrokerStore`, and `VerificationStore` protocols. `Plane.from_store(...)` validates
all required operations before startup, so an incomplete backend fails early.

`SQLitePlaneStore` implements the contract while preserving the historical
`ClaimRegistry` API. A PostgreSQL adapter does not need to inherit from SQLite,
but it must preserve the same atomic admission, lease/fencing, operation-CAS,
observation-chain, invalidation, and verification semantics.
