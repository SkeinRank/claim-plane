# Trusted execution

Claim Plane separates four properties that are often incorrectly conflated:

1. **integrity** — recorded evidence was not altered;
2. **coverage** — the monitor claims to observe a class of operations;
3. **enforcement** — disallowed operations are blocked before execution;
4. **isolation** — the worker cannot bypass the monitor through another channel.

## Governed admission

Governed mode is the default. Every `ChangeIntent` must carry a full Git object ID in `base_commit` before admission. A branch name remains audit metadata only.

## Trusted sessions

Hash-chained HMAC sessions protect sequence, payload, monitor identity, coverage, completeness, and the final head hash. They make tampering detectable, but a generic `tool_proxy` session still depends on the runtime honestly routing every action through that proxy.

## Brokered sessions

`brokered_proxy` coverage is issued by the Claim Plane repository broker. The broker checks intent permissions and performs the file operation itself. Integration with `ObservationPolicy(mode="brokered")` accepts only sealed broker sessions whose events carry the broker protocol marker.

This is stronger than a worker-owned JSONL trace or a generic tool callback. Completeness still depends on the worker being isolated from alternate repository or host channels.

## Sandboxes

- `tree`: repository mutation detection, not process isolation;
- `bwrap`: compatibility profile with host filesystem mounted read-only;
- `bwrap-minimal`: minimal Linux namespace with explicit read/write roots;
- `sandbox-exec`: macOS best-effort policy;
- `auto`: prefers `bwrap-minimal`, then `sandbox-exec`.

Policies may declare readable paths, writable paths, environment-variable allowlists, network access, and whether the repository itself is writable.

## Evidence

Evidence binds exact worker patches, manifests, result trees, result commits, observation-session digests, acceptance integrity, and signatures. Runtime provenance additionally records:

- Claim Plane package-source digest;
- installed schema-bundle digest when available;
- complete sandbox/observation policy digest;
- executable, Python, and platform identity.

HMAC is appropriate inside one CI trust domain. Ed25519 enables verification with a public key. Neither signature proves that a deployment used a non-bypassable monitor; the monitor and sandbox policy remain explicit parts of the attested provenance.


## Broker operation provenance

Brokered evidence additionally binds the registered broker instance, intent content version, repository identity, policy digest, binary digest, and signed operation prepare/commit records. The observation key authenticates the event chain; the separate broker key authenticates the reference monitor and operation journal.
