# Guarantees and trust boundaries

Claim Plane separates runtime enforcement from evidence integrity. A report must state
what was blocked before mutation, what was observed, what was verified only after
execution, and what was outside adapter visibility.

## What a verified controlled run establishes

For the Git state bound to the run, Claim Plane verifies that:

- the adapter, runtime, protocol, project, policy, session, and intent identities match;
- the lifecycle event chain is ordered, complete for the recorded boundary, and untampered;
- completed mutations are covered by initial or explicitly amended authority;
- stale, cancelled, expired, or corrupt authority does not become an implicit allow;
- the final tracked and relevant untracked changes match the verified Git state;
- configured acceptance commands and deterministic policy checks produced the recorded result.

## What it does not establish

A verified result does not prove that:

- business logic is correct for every input;
- tests are complete;
- the implementation is free of security vulnerabilities;
- project-local hooks form a non-bypassable operating-system sandbox;
- a human review is unnecessary for important code.

## Codex boundary

Project-local Codex hooks can hard-block the supported mutations routed through those
hooks. Direct host writes or activity outside runtime visibility are post-verified by the
final Git comparison. `strict` and `critical` policies refuse to start when their required
guarantees are unavailable.

Use the Linux brokered boundary when non-bypassable repository mutation isolation is
required. On macOS, run that boundary in a Linux VM or container rather than representing
project hooks as operating-system enforcement.

## Evidence hygiene

Normalized lifecycle evidence excludes raw prompts, source content, credentials, tool
payloads, hook output, and final model messages. Digests and structured metadata are
used where the raw value is not required for verification.
