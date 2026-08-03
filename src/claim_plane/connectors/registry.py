"""Built-in adapter registrations and third-party adapter discovery."""

from __future__ import annotations

from claim_plane.connectors.codex_adapter import CodexAdapter
from claim_plane.protocol.registry import AdapterRegistry, AdapterSource


def build_adapter_registry(*, discover_external: bool = True) -> AdapterRegistry:
    """Return the default registry without coupling external adapters to core code."""

    registry = AdapterRegistry()
    registry.register(
        CodexAdapter.name,
        CodexAdapter,
        protocol_range=CodexAdapter.supported_protocol_range,
        source=AdapterSource.BUILTIN,
        metadata={"role": "first-party reference runtime"},
    )
    if discover_external:
        registry.discover_entry_points()
    return registry


__all__ = ["build_adapter_registry"]
