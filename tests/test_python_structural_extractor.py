"""Python structural extraction into Semantic Resource IR v2."""

from __future__ import annotations

from pathlib import Path

import pytest

from claim_plane import (
    PYTHON_STRUCTURAL_INDEX_PROTOCOL,
    PythonStructuralExtractionError,
    PythonStructuralIndex,
    PythonSymbolKind,
    ResourceLayer,
    extract_python_file,
    extract_python_files,
    extract_python_structure,
)


SOURCE = '''\
from typing import Iterable


class Parser(BaseParser):
    mode = "strict"

    @property
    def name(self) -> str:
        return "parser"

    def validate(self, value: str, *, strict: bool = False) -> bool:
        def normalized() -> str:
            return value.strip()

        return bool(normalized()) if strict else True

    async def parse(self, values: Iterable[str]) -> list[str]:
        return list(values)


def helper(value: str = "x") -> str:
    return value
'''


def test_extracts_stable_python_symbols_signatures_and_lexical_owners() -> None:
    index = extract_python_structure(SOURCE, path="src/parser.py")

    assert index.protocol == PYTHON_STRUCTURAL_INDEX_PROTOCOL
    assert index.file_resource.identity == "file:src/parser.py"
    assert index.file_resource.language == "python"
    by_name = {item.qualified_name: item for item in index.definitions}
    assert set(by_name) == {
        "Parser",
        "Parser.name",
        "Parser.validate",
        "Parser.validate.normalized",
        "Parser.parse",
        "helper",
    }

    parser = by_name["Parser"]
    validate = by_name["Parser.validate"]
    nested = by_name["Parser.validate.normalized"]
    parse = by_name["Parser.parse"]

    assert parser.symbol_kind is PythonSymbolKind.CLASS
    assert parser.resource.signature == "Parser(BaseParser)"
    assert parser.owner_identity is None
    assert validate.symbol_kind is PythonSymbolKind.METHOD
    assert validate.owner_qualified_name == "Parser"
    assert validate.owner_identity == parser.resource.identity
    assert validate.resource.signature == (
        "Parser.validate(self, value: str, *, strict: bool = False) -> bool"
    )
    assert validate.resource.layer is ResourceLayer.SYMBOL
    assert validate.resource.region == validate.region
    assert validate.resource.metadata["body_region"] == validate.body_region
    assert nested.symbol_kind is PythonSymbolKind.FUNCTION
    assert nested.owner_qualified_name == "Parser.validate"
    assert parse.symbol_kind is PythonSymbolKind.ASYNC_METHOD
    assert by_name["Parser.name"].decorators == ("property",)


def test_symbol_identity_survives_line_movement_and_signature_evolution() -> None:
    before = extract_python_structure(
        (
            "class Parser:\n"
            "    def validate(self, value: str) -> bool:\n"
            "        return True\n"
        ),
        path="src/parser.py",
    )
    after = extract_python_structure(
        (
            "\n\nclass Parser:\n"
            "    def validate(self, value: str, strict: bool = False) -> bool:\n"
            "        return True\n"
        ),
        path="src/parser.py",
    )

    old = before.definitions_for_symbol("Parser.validate")[0]
    new = after.definitions_for_symbol("Parser.validate")[0]
    assert old.resource.identity == "symbol:src/parser.py#Parser.validate"
    assert old.resource.stable_id == new.resource.stable_id
    assert old.resource.region != new.resource.region
    assert old.resource.signature != new.resource.signature


def test_line_ownership_prefers_the_deepest_lexical_definition() -> None:
    index = extract_python_structure(SOURCE, path="src/parser.py")
    by_name = {item.qualified_name: item for item in index.definitions}

    validate = by_name["Parser.validate"]
    nested = by_name["Parser.validate.normalized"]
    parser = by_name["Parser"]

    assert index.owner_for_line(nested.definition_line).qualified_name == (
        "Parser.validate.normalized"
    )
    assert index.owner_for_line(validate.definition_line).qualified_name == (
        "Parser.validate"
    )
    assert index.owner_for_line(parser.definition_line).qualified_name == "Parser"
    assert index.owner_for_line(1).identity == "file:src/parser.py"

    owners = index.owners_for_lines(
        [validate.definition_line, by_name["Parser.parse"].definition_line]
    )
    assert [item.qualified_name for item in owners] == [
        "Parser.validate",
        "Parser.parse",
    ]


def test_decorator_lines_are_owned_by_the_decorated_symbol() -> None:
    index = extract_python_structure(SOURCE, path="src/parser.py")
    prop = index.definitions_for_symbol("Parser.name")[0]

    assert prop.definition_start_line < prop.definition_line
    assert (
        index.owner_for_line(prop.definition_start_line).qualified_name == "Parser.name"
    )


def test_repeated_logical_definitions_share_semantic_identity_but_keep_occurrences(
) -> None:
    source = '''\
from typing import overload

@overload
def render(value: int) -> str: ...

@overload
def render(value: str) -> str: ...

def render(value: object) -> str:
    return str(value)
'''
    index = extract_python_structure(source, path="src/render.py")
    definitions = index.definitions_for_symbol("render")

    assert [item.occurrence for item in definitions] == [1, 2, 3]
    assert len({item.resource.stable_id for item in definitions}) == 1
    assert index.owner_for_line(definitions[1].definition_line).stable_id == (
        definitions[0].resource.stable_id
    )


def test_index_round_trip_and_fingerprint_are_deterministic() -> None:
    first = extract_python_structure(SOURCE, path="src/parser.py")
    second = extract_python_structure(SOURCE, path=r"src\\parser.py")
    restored = PythonStructuralIndex.from_dict(first.to_dict())

    assert restored == first
    assert first.to_dict() == second.to_dict()
    assert first.fingerprint() == second.fingerprint()


def test_syntax_errors_fail_closed_with_source_coordinates() -> None:
    with pytest.raises(PythonStructuralExtractionError, match=r"src/broken.py:1"):
        extract_python_structure("def broken(:\n    pass\n", path="src/broken.py")


def test_repository_file_extraction_is_encoding_aware_and_root_bounded(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source_dir = repo / "src"
    source_dir.mkdir(parents=True)
    target = source_dir / "latin.py"
    target.write_bytes(
        "# -*- coding: latin-1 -*-\ndef café():\n    return 'ok'\n".encode("latin-1")
    )

    index = extract_python_file(target, repository_root=repo)
    assert index.path == "src/latin.py"
    assert index.definitions[0].qualified_name == "café"

    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inside repository_root"):
        extract_python_file(outside, repository_root=repo)


def test_multi_file_extraction_is_path_sorted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
    (repo / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")

    indexes = extract_python_files(["b.py", "a.py"], repository_root=repo)
    assert [item.path for item in indexes] == ["a.py", "b.py"]
