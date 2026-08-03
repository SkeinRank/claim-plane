# Security policy

## Project status

Claim Plane is a Technical Preview. The broker, evidence pipeline, and sandbox adapters
are designed to make trust boundaries explicit, but the project is not yet presented as
a complete production security boundary.

The default SQLite backend and canonical OS worktree lock provide a single-host model.
Multi-host deployments require a network-authoritative store, distributed leases, and
fencing while retaining local host locks.

## Coding-agent connector boundary

Project-local Codex hooks provide deterministic interception for the tool surfaces that
the Codex runtime dispatches through those hooks. They are not presented as a
non-bypassable operating-system security boundary. Deployments that require repository
mutation isolation should use Claim Plane's brokered execution boundary in an isolated
Linux environment.

Codex scope-amendment tickets are short-lived and bind one session, active intent
fingerprint, pinned base commit, and exact denied mutation set. The model supplies only
a rationale; it cannot select wider resources through the amendment command. Normal
admission policy still decides whether the amendment is allowed.

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
