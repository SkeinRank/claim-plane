"""Packaged schemas and technical-preview resource helpers."""

from __future__ import annotations

import hashlib
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

RESOURCE_PROTOCOL = "claim-plane.packaged-resources.v1"


def schema_root():
    """Return the traversable directory containing packaged public JSON Schemas."""

    return files("claim_plane.resources").joinpath("schemas")


def list_schemas() -> tuple[dict[str, Any], ...]:
    """List packaged schemas with stable names and digests."""

    items: list[dict[str, Any]] = []
    root = schema_root()
    for resource in sorted(root.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".json"):
            continue
        data = resource.read_bytes()
        items.append(
            {
                "name": resource.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return tuple(items)


def export_schemas(destination: str | Path) -> dict[str, Any]:
    """Export the exact packaged schemas into an existing or new directory."""

    target = Path(destination).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    exported: list[str] = []
    root = schema_root()
    for item in list_schemas():
        name = str(item["name"])
        source = root.joinpath(name)
        output = target / name
        with source.open("rb") as src, output.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        exported.append(str(output))
    return {
        "protocol": RESOURCE_PROTOCOL,
        "destination": str(target),
        "count": len(exported),
        "files": exported,
    }
