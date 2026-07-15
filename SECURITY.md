# Security policy

## Project status

Claim Plane is a Research Preview. The broker, evidence pipeline, and sandbox adapters
are designed to make trust boundaries explicit, but the project is not yet presented as
a complete production security boundary.

The default SQLite backend and canonical OS worktree lock provide a single-host model.
Multi-host deployments require a network-authoritative store, distributed leases, and
fencing while retaining local host locks.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or a private Security Advisory for
the repository. Do not open a public issue for a suspected vulnerability until a fix or
coordinated disclosure plan is available.

Include:

- affected Claim Plane version or commit;
- operating system and Python version;
- deployment mode and sandbox backend;
- minimal reproduction steps;
- the expected and observed trust-boundary behavior.

## Supported versions

Before 1.0, security fixes are provided for the latest published `0.x` release. Users
should upgrade to the newest release rather than relying on long-lived support branches.
