from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from claim_plane.code_intelligence import (
    ScipArtifactMismatch,
    ScipDecodeError,
    ScipIndexArtifact,
    build_scip_semantic_resource_index,
)


def _varint(value: int) -> bytes:
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            output.append(byte | 0x80)
        else:
            output.append(byte)
            return bytes(output)


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_text(number: int, value: str) -> bytes:
    return _field_bytes(number, value.encode("utf-8"))


def _single_line(line: int, start: int, end: int) -> bytes:
    return b"".join(
        (
            _field_varint(1, line),
            _field_varint(2, start),
            _field_varint(3, end),
        )
    )


def _occurrence(
    symbol: str,
    *,
    line: int,
    start: int,
    end: int,
    roles: int,
    legacy_range: tuple[int, ...] | None = None,
) -> bytes:
    fields = []
    if legacy_range is not None:
        packed = b"".join(_varint(item) for item in legacy_range)
        fields.append(_field_bytes(1, packed))
    fields.extend(
        (
            _field_text(2, symbol),
            _field_varint(3, roles),
            _field_bytes(8, _single_line(line, start, end)),
        )
    )
    return b"".join(fields)


def _relationship(
    symbol: str,
    *,
    reference: bool = False,
    implementation: bool = False,
    type_definition: bool = False,
    definition: bool = False,
) -> bytes:
    return b"".join(
        (
            _field_text(1, symbol),
            _field_varint(2, int(reference)),
            _field_varint(3, int(implementation)),
            _field_varint(4, int(type_definition)),
            _field_varint(5, int(definition)),
        )
    )


def _symbol(
    symbol: str,
    *,
    display_name: str,
    kind: int,
    signature: str | None = None,
    relationships: tuple[bytes, ...] = (),
) -> bytes:
    fields = [_field_text(1, symbol), _field_text(3, "fixture docs")]
    fields.extend(_field_bytes(4, item) for item in relationships)
    fields.extend((_field_varint(5, kind), _field_text(6, display_name)))
    if signature is not None:
        signature_message = _field_text(4, "python") + _field_text(5, signature)
        fields.append(_field_bytes(7, signature_message))
    return b"".join(fields)


def _fixture_index(*, revision: str) -> tuple[bytes, str, str]:
    worker = f"scip-python pip demo {revision} demo/app/Worker#"
    run = f"scip-python pip demo {revision} demo/app/Worker#run()."
    external = "scip-python pip dependency 9.1 dependency/Base#run()."

    tool = b"".join(
        (
            _field_text(1, "scip-python"),
            _field_text(2, "0.6.6"),
            _field_text(3, "index"),
        )
    )
    metadata = _field_bytes(2, tool) + _field_text(3, "file:///repo")

    document = b"".join(
        (
            _field_text(1, "src/app.py"),
            _field_bytes(
                2,
                _occurrence(
                    worker,
                    line=2,
                    start=6,
                    end=12,
                    roles=0x1,
                    legacy_range=(99, 1, 2),
                ),
            ),
            _field_bytes(
                2,
                _occurrence(run, line=3, start=8, end=11, roles=0x1 | 0x4),
            ),
            _field_bytes(
                2,
                _occurrence(external, line=4, start=11, end=14, roles=0x8),
            ),
            _field_bytes(
                2,
                _occurrence("local 1", line=5, start=4, end=9, roles=0x4),
            ),
            _field_bytes(3, _symbol(worker, display_name="Worker", kind=7)),
            _field_bytes(
                3,
                _symbol(
                    run,
                    display_name="run",
                    kind=26,
                    signature="def run(self) -> str",
                    relationships=(
                        _relationship(
                            external,
                            reference=True,
                            implementation=True,
                        ),
                    ),
                ),
            ),
            _field_text(4, "python"),
            _field_varint(6, 3),
        )
    )
    external_symbol = _symbol(external, display_name="run", kind=26)
    index = b"".join(
        (
            _field_bytes(1, metadata),
            _field_bytes(2, document),
            _field_bytes(3, external_symbol),
        )
    )
    return index, run, external


def _artifact(
    tmp_path: Path, *, revision: str = "a" * 40
) -> tuple[ScipIndexArtifact, str, str]:
    payload, run, external = _fixture_index(revision=revision)
    path = tmp_path / "index.scip"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return (
        ScipIndexArtifact(
            index_path=path,
            cache_key="c" * 64,
            repository_root=tmp_path,
            revision=revision,
            workspace_fingerprint="w" * 64,
            dirty=False,
            indexer_id="scip-python",
            indexer_version="0.6.6",
            environment_fingerprint="e" * 64,
            project_name="demo",
            project_version=revision,
            sha256=digest,
            size_bytes=len(payload),
            cache_hit=False,
        ),
        run,
        external,
    )


def test_scip_symbols_become_stable_semantic_resources(tmp_path: Path) -> None:
    artifact, run, external = _artifact(tmp_path)

    index = build_scip_semantic_resource_index(artifact)

    assert index.revision == artifact.revision
    assert index.tool_name == "scip-python"
    assert index.tool_version == "0.6.6"
    assert [resource.identity for resource in index.file_resources] == [
        "file:src/app.py"
    ]
    symbols = {item.scip_symbol: item for item in index.symbols}
    assert symbols[run].resource.identity == "symbol:src/app.py#demo/app/Worker#run()."
    assert symbols[run].resource.signature == "def run(self) -> str"
    assert symbols[run].resource.metadata["scip_kind"] == "method"
    assert symbols[run].resource.metadata["scip_package_version"] == artifact.revision
    assert symbols[external].external is True
    assert symbols[external].resource.identity == (
        "symbol:external:scip-python:pip:dependency:dependency/Base#run()."
    )
    assert all(item.scip_symbol != "local 1" for item in index.symbols)


def test_local_project_identity_excludes_revision(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first, first_run, _ = _artifact(first_dir, revision="a" * 40)
    second, second_run, _ = _artifact(second_dir, revision="b" * 40)

    first_index = build_scip_semantic_resource_index(first)
    second_index = build_scip_semantic_resource_index(second)
    first_resource = next(
        item.resource for item in first_index.symbols if item.scip_symbol == first_run
    )
    second_resource = next(
        item.resource for item in second_index.symbols if item.scip_symbol == second_run
    )

    assert first_resource.identity == second_resource.identity
    assert first_resource.stable_id == second_resource.stable_id
    assert first_resource.metadata["scip_package_version"] != second_resource.metadata[
        "scip_package_version"
    ]


def test_occurrences_and_relationships_keep_revision_bound_evidence(
    tmp_path: Path,
) -> None:
    artifact, run, external = _artifact(tmp_path)

    index = build_scip_semantic_resource_index(artifact)

    worker_occurrence = next(
        item
        for item in index.occurrences
        if "Worker#" in item.scip_symbol and "run" not in item.scip_symbol
    )
    assert worker_occurrence.source_range is not None
    assert worker_occurrence.source_range.start_line == 2
    assert worker_occurrence.source_range.start_character == 6
    assert worker_occurrence.role_names == ("definition",)

    local = next(item for item in index.occurrences if item.scip_symbol == "local 1")
    assert local.resource_stable_id is None

    relationship = next(
        item for item in index.relationships if item.source_symbol == run
    )
    assert relationship.target_symbol == external
    assert relationship.is_reference is True
    assert relationship.is_implementation is True
    assert relationship.source_resource_stable_id is not None
    assert relationship.target_resource_stable_id is not None


def test_projection_is_deterministic_and_can_exclude_external_symbols(
    tmp_path: Path,
) -> None:
    artifact, _, external = _artifact(tmp_path)

    first = build_scip_semantic_resource_index(artifact)
    second = build_scip_semantic_resource_index(artifact)
    local_only = build_scip_semantic_resource_index(
        artifact, include_external_symbols=False
    )

    assert first.fingerprint == second.fingerprint
    assert first.to_dict() == second.to_dict()
    assert all(item.scip_symbol != external for item in local_only.symbols)
    relation = local_only.relationships[0]
    assert relation.target_symbol == external
    assert relation.target_resource_stable_id is None


def test_artifact_integrity_is_rechecked_before_decoding(tmp_path: Path) -> None:
    artifact, _, _ = _artifact(tmp_path)
    artifact.index_path.write_bytes(artifact.index_path.read_bytes() + b"tampered")

    with pytest.raises(ScipArtifactMismatch, match="sealed cache metadata"):
        build_scip_semantic_resource_index(artifact)


def test_malformed_protobuf_and_invalid_document_paths_fail_closed(
    tmp_path: Path,
) -> None:
    artifact, _, _ = _artifact(tmp_path)
    malformed = b"\x12\x80"
    artifact.index_path.write_bytes(malformed)
    object.__setattr__(artifact, "sha256", hashlib.sha256(malformed).hexdigest())
    object.__setattr__(artifact, "size_bytes", len(malformed))

    with pytest.raises(ScipDecodeError):
        build_scip_semantic_resource_index(artifact)

    document = _field_text(1, "../escape.py") + _field_text(4, "python")
    invalid_path = _field_bytes(2, document)
    artifact.index_path.write_bytes(invalid_path)
    object.__setattr__(artifact, "sha256", hashlib.sha256(invalid_path).hexdigest())
    object.__setattr__(artifact, "size_bytes", len(invalid_path))

    with pytest.raises(ScipDecodeError, match="relative_path"):
        build_scip_semantic_resource_index(artifact)
