"""Collect concrete files, Git hunks, typed contracts, and terminology evidence."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from claim_plane.core.extract import extract_artifacts
from claim_plane.core.models import (
    ChangeIntent,
    ChangeManifest,
    ChangedRegion,
    ClaimType,
    ObservedArtifact,
    ResourceKind,
)
from claim_plane.core.semantic import SemanticIdentityResolver

_HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)


class GitChangeCollector:
    def __init__(
        self, semantic_resolver: SemanticIdentityResolver | None = None
    ) -> None:
        self._semantic = semantic_resolver

    def collect(self, repo_path: str | Path, intent: ChangeIntent) -> ChangeManifest:
        repo = Path(repo_path).resolve()
        if not self._git(repo, "rev-parse", "--git-dir", check=False).strip():
            raise ValueError(f"not a Git worktree: {repo}")

        head = self._git(repo, "rev-parse", "HEAD").strip()
        base_ref = intent.base_commit or intent.base_revision
        resolved_base = self._git(
            repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}", check=False
        ).strip()
        merge_base = self._git(
            repo, "merge-base", base_ref, "HEAD", check=False
        ).strip()
        base_is_ancestor = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", base_ref, "HEAD"],
                cwd=repo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )
        names = self._git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=ACMRDT",
            base_ref,
            "--",
            check=False,
        )
        changed_files = tuple(
            line.strip() for line in names.splitlines() if line.strip()
        )
        patch = self._git(
            repo,
            "diff",
            "--no-color",
            "--unified=0",
            "--diff-filter=ACMRDT",
            base_ref,
            "--",
            check=False,
        )
        changed_regions = tuple(_parse_changed_regions(patch))
        regions_by_path: dict[str, list[ChangedRegion]] = {}
        for region in changed_regions:
            regions_by_path.setdefault(region.path, []).append(region)

        observed: list[ObservedArtifact] = []
        for relative in changed_files:
            path = repo / relative
            if not path.exists() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            path_regions = regions_by_path.get(relative, [])
            if path.suffix == ".py":
                observed.extend(self._python_artifacts(relative, text, path_regions))
            elif path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"}:
                observed.extend(self._document_artifacts(relative, text, path_regions))

        preserve_specs = _preserved_contract_specs(intent)
        if preserve_specs:
            observed.extend(self._preserved_contract_artifacts(repo, preserve_specs))

        deduplicated: dict[tuple[object, ...], ObservedArtifact] = {}
        for artifact in observed:
            marker = (
                artifact.kind,
                artifact.path,
                artifact.qualified_identifier or artifact.identifier,
                artifact.signature,
                artifact.subject_concept_id,
            )
            current = deduplicated.get(marker)
            if current is None or current.metadata.get("inventory_only"):
                deduplicated[marker] = artifact

        return ChangeManifest(
            intent_id=intent.intent_id,
            owner=intent.owner,
            base_revision=intent.base_revision,
            base_commit=resolved_base or None,
            changed_files=changed_files,
            artifacts=tuple(deduplicated.values()),
            changed_regions=changed_regions,
            metadata={
                "repo_path": str(repo),
                "head_revision": head,
                "resolved_base_commit": resolved_base or None,
                "merge_base": merge_base or None,
                "base_is_ancestor": base_is_ancestor,
                "collector": "git-diff-unified-0",
            },
        )

    def _python_artifacts(
        self, relative: str, text: str, regions: list[ChangedRegion]
    ) -> list[ObservedArtifact]:
        result: list[ObservedArtifact] = []
        for artifact in extract_artifacts(text, path=relative):
            if (
                artifact.line_start is not None
                and regions
                and not any(
                    region.overlaps(
                        artifact.line_start, artifact.line_end or artifact.line_start
                    )
                    for region in regions
                )
            ):
                continue
            kind = (
                ResourceKind.CONTRACT
                if artifact.kind is ClaimType.CONTRACT
                else ResourceKind.SYMBOL
            )
            concept_id = None
            subject_concept_id = None
            metadata: dict[str, object] = {}
            if self._semantic is not None:
                resolution = self._semantic.resolve(artifact.identifier)
                concept_id = resolution.concept_id
                metadata.update(
                    {
                        "semantic_status": resolution.status,
                        "canonical": resolution.canonical,
                        "deprecated": resolution.deprecated,
                    }
                )
                if artifact.subject_identifier:
                    subject_resolution = self._semantic.resolve(
                        artifact.subject_identifier
                    )
                    subject_concept_id = (
                        subject_resolution.concept_id or artifact.subject_identifier
                    )
            elif artifact.subject_identifier:
                subject_concept_id = artifact.subject_identifier
            result.append(
                ObservedArtifact(
                    kind=kind,
                    identifier=artifact.identifier,
                    signature=artifact.signature,
                    path=relative,
                    concept_id=concept_id,
                    subject_concept_id=subject_concept_id,
                    qualified_identifier=artifact.qualified_identifier,
                    line_start=artifact.line_start,
                    line_end=artifact.line_end,
                    metadata=metadata,
                )
            )
        return result

    def _preserved_contract_artifacts(
        self, repo: Path, specs: set[tuple[str | None, str]]
    ) -> list[ObservedArtifact]:
        tracked = self._git(repo, "ls-files", "*.py", check=False)
        result: list[ObservedArtifact] = []
        for relative in (line.strip() for line in tracked.splitlines() if line.strip()):
            path = repo / relative
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for artifact in extract_artifacts(text, path=relative):
                if artifact.kind is not ClaimType.CONTRACT:
                    continue
                if not any(
                    _preserve_matches(
                        spec,
                        identifier=artifact.identifier,
                        qualified_identifier=artifact.qualified_identifier,
                        subject_identifier=artifact.subject_identifier,
                    )
                    for spec in specs
                ):
                    continue
                subject_concept_id = artifact.subject_identifier
                concept_id = None
                metadata: dict[str, object] = {"inventory_only": True}
                if self._semantic is not None:
                    resolution = self._semantic.resolve(artifact.identifier)
                    concept_id = resolution.concept_id
                    metadata.update(
                        {
                            "semantic_status": resolution.status,
                            "canonical": resolution.canonical,
                            "deprecated": resolution.deprecated,
                        }
                    )
                    if artifact.subject_identifier:
                        subject_resolution = self._semantic.resolve(
                            artifact.subject_identifier
                        )
                        subject_concept_id = (
                            subject_resolution.concept_id or artifact.subject_identifier
                        )
                result.append(
                    ObservedArtifact(
                        kind=ResourceKind.CONTRACT,
                        identifier=artifact.identifier,
                        signature=artifact.signature,
                        path=relative,
                        concept_id=concept_id,
                        subject_concept_id=subject_concept_id,
                        qualified_identifier=artifact.qualified_identifier,
                        line_start=artifact.line_start,
                        line_end=artifact.line_end,
                        metadata=metadata,
                    )
                )
        return result

    def _document_artifacts(
        self, relative: str, text: str, regions: list[ChangedRegion]
    ) -> list[ObservedArtifact]:
        if self._semantic is None or not self._semantic.enabled:
            return []
        snippets: list[tuple[str, int]] = []
        lines = text.splitlines(keepends=True)
        if regions:
            for region in regions:
                start = max(region.start_line - 1, 0)
                end = min(region.end_line, len(lines))
                snippets.append(("".join(lines[start:end]), start + 1))
        else:
            snippets.append((text, 1))
        result: list[ObservedArtifact] = []
        for snippet, base_line in snippets:
            for match in self._semantic.scan_text(snippet):
                line = base_line + snippet[: int(match["start"])].count("\n")
                result.append(
                    ObservedArtifact(
                        kind=ResourceKind.CONCEPT,
                        identifier=str(match["matched_text"]),
                        path=relative,
                        concept_id=str(match["term_id"]),
                        line_start=line,
                        line_end=line,
                        metadata={
                            "surface": match["surface"],
                            "surface_kind": match["kind"],
                            "deprecated": bool(match["deprecated"]),
                            "documentation": True,
                        },
                    )
                )
        return result

    @staticmethod
    def _git(repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout


def _parse_changed_regions(patch: str) -> list[ChangedRegion]:
    regions: list[ChangedRegion] = []
    current_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            value = line[4:].strip()
            if value == "/dev/null":
                current_path = None
            elif value.startswith("b/"):
                current_path = value[2:]
            else:
                current_path = value
            continue
        match = _HUNK_RE.match(line)
        if not match or not current_path:
            continue
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_start = int(match.group("new_start"))
        new_count = int(match.group("new_count") or "1")
        start = max(1, new_start)
        end = start if new_count == 0 else new_start + new_count - 1
        old_end = old_start if old_count == 0 else old_start + old_count - 1
        regions.append(
            ChangedRegion(
                path=current_path,
                start_line=start,
                end_line=max(start, end),
                old_start_line=old_start,
                old_end_line=max(old_start, old_end),
            )
        )
    return regions


def _preserved_contract_specs(intent: ChangeIntent) -> set[tuple[str | None, str]]:
    specs: set[tuple[str | None, str]] = set()
    for policy in intent.preserves:
        if not policy.startswith("contract:") or "=" not in policy:
            continue
        left = policy.split(":", 1)[1].split("=", 1)[0].strip()
        if "::" in left:
            subject, identifier = left.split("::", 1)
            specs.add((subject.strip(), identifier.strip()))
        else:
            specs.add((None, left))
    return specs


def _preserve_matches(
    spec: tuple[str | None, str],
    *,
    identifier: str,
    qualified_identifier: str | None,
    subject_identifier: str | None,
) -> bool:
    subject, expected_identifier = spec
    identifier_matches = expected_identifier in {identifier, qualified_identifier}
    if not identifier_matches:
        return False
    return subject is None or _norm(subject) == _norm(subject_identifier or "")


def _norm(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())
