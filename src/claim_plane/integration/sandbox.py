"""Best-effort and strict process isolation for acceptance commands.

Backends:

``tree``
    Runs normally and relies on repository-tree mutation detection.
``bwrap``
    Legacy host-readonly Bubblewrap profile retained for compatibility.
``bwrap-minimal``
    A minimal Linux namespace exposing only selected runtime roots, the
    repository, ``/tmp``, ``/proc`` and ``/dev``.  Host home directories and
    unrelated repositories are absent unless explicitly allowlisted.
``sandbox-exec``
    macOS best-effort profile.  This backend is deprecated by Apple and cannot
    provide the same namespace guarantees as Bubblewrap.
``auto``
    Prefers ``bwrap-minimal`` and then ``sandbox-exec``.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    backend: str = "tree"
    allow_network: bool = False
    strict: bool = False
    writable_paths: tuple[str, ...] = ()
    readable_paths: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = ()
    repository_writable: bool = True

    def __post_init__(self) -> None:
        allowed = {
            "tree",
            "auto",
            "bwrap",
            "bwrap-minimal",
            "sandbox-exec",
            "none",
        }
        if self.backend not in allowed:
            raise ValueError(f"unsupported sandbox backend: {self.backend}")
        object.__setattr__(
            self,
            "writable_paths",
            tuple(dict.fromkeys(str(item) for item in self.writable_paths)),
        )
        object.__setattr__(
            self,
            "readable_paths",
            tuple(dict.fromkeys(str(item) for item in self.readable_paths)),
        )
        object.__setattr__(
            self,
            "environment_allowlist",
            tuple(dict.fromkeys(str(item) for item in self.environment_allowlist)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "allow_network": self.allow_network,
            "strict": self.strict,
            "writable_paths": list(self.writable_paths),
            "readable_paths": list(self.readable_paths),
            "environment_allowlist": list(self.environment_allowlist),
            "repository_writable": self.repository_writable,
        }

    @classmethod
    def from_dict(cls, data: object | None) -> "SandboxPolicy":
        if data is None:
            return cls()
        if isinstance(data, str):
            return cls(backend=data, strict=data not in {"tree", "none"})
        if not isinstance(data, dict):
            raise TypeError("sandbox policy must be an object or backend name")
        return cls(
            backend=str(data.get("backend") or "tree"),
            allow_network=bool(data.get("allow_network", False)),
            strict=bool(data.get("strict", False)),
            writable_paths=tuple(
                str(item) for item in data.get("writable_paths") or ()
            ),
            readable_paths=tuple(
                str(item) for item in data.get("readable_paths") or ()
            ),
            environment_allowlist=tuple(
                str(item) for item in data.get("environment_allowlist") or ()
            ),
            repository_writable=bool(data.get("repository_writable", True)),
        )


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    argv: tuple[str, ...]
    backend: str
    enforced: bool


def _minimal_runtime_roots(policy: SandboxPolicy) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for value in (
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc/ssl",
        "/etc/hosts",
        "/etc/resolv.conf",
    ):
        path = Path(value)
        if path.exists():
            candidates.append(path.resolve())
    prefixes = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()}
    for prefix in prefixes:
        if prefix.exists() and not any(
            prefix == root or root in prefix.parents for root in candidates
        ):
            candidates.append(prefix)
    candidates.extend(
        Path(item).expanduser().resolve()
        for item in policy.readable_paths
        if Path(item).expanduser().exists()
    )
    return tuple(dict.fromkeys(candidates))


def _bwrap_minimal(
    command: str,
    root: Path,
    policy: SandboxPolicy,
) -> SandboxCommand:
    argv: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net" if policy.allow_network else "--unshare-net",
        "--tmpfs",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/workspace",
    ]
    for runtime_root in _minimal_runtime_roots(policy):
        argv.extend(("--ro-bind", str(runtime_root), str(runtime_root)))
    bind_flag = "--bind" if policy.repository_writable else "--ro-bind"
    argv.extend((bind_flag, str(root), "/workspace", "--chdir", "/workspace"))
    for value in policy.writable_paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        argv.extend(("--bind", str(path), str(path)))
    argv.extend(("/bin/sh", "-lc", command))
    return SandboxCommand(tuple(argv), "bwrap-minimal", True)


def resolve_sandbox_command(
    command: str,
    cwd: str | Path,
    policy: SandboxPolicy,
) -> SandboxCommand:
    root = Path(cwd).resolve()
    requested = policy.backend
    if requested in {"tree", "none"}:
        return SandboxCommand(("/bin/sh", "-lc", command), requested, False)

    candidates: Sequence[str]
    if requested == "auto":
        candidates = ("bwrap-minimal", "sandbox-exec")
    else:
        candidates = (requested,)

    for backend in candidates:
        if backend == "bwrap-minimal" and shutil.which("bwrap"):
            return _bwrap_minimal(command, root, policy)
        if backend == "bwrap" and shutil.which("bwrap"):
            argv = [
                "bwrap",
                "--die-with-parent",
                "--new-session",
                "--ro-bind",
                "/",
                "/",
                "--bind" if policy.repository_writable else "--ro-bind",
                str(root),
                str(root),
                "--chdir",
                str(root),
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
            ]
            if not policy.allow_network:
                argv.append("--unshare-net")
            for value in policy.writable_paths:
                path = Path(value).expanduser().resolve()
                argv.extend(("--bind", str(path), str(path)))
            argv.extend(("/bin/sh", "-lc", command))
            return SandboxCommand(tuple(argv), "bwrap", True)
        if backend == "sandbox-exec" and shutil.which("sandbox-exec"):
            writable = ([str(root)] if policy.repository_writable else []) + [
                "/tmp",
                *policy.writable_paths,
            ]
            clauses = [
                "(version 1)",
                "(deny default)",
                "(allow process*)",
                '(allow file-read* (subpath "/usr"))',
                '(allow file-read* (subpath "/System"))',
                '(allow file-read* (subpath "/Library"))',
                f'(allow file-read* (subpath "{root}"))',
            ]
            for value in policy.readable_paths:
                clauses.append(
                    f'(allow file-read* (subpath "{Path(value).expanduser().resolve()}"))'
                )
            for value in writable:
                clauses.append(
                    f'(allow file-write* (subpath "{Path(value).expanduser().resolve()}"))'
                )
            if policy.allow_network:
                clauses.append("(allow network*)")
            profile = " ".join(clauses)
            return SandboxCommand(
                ("sandbox-exec", "-p", profile, "/bin/sh", "-lc", command),
                "sandbox-exec",
                True,
            )

    if policy.strict:
        system = platform.system()
        raise RuntimeError(
            f"strict sandbox requested but no supported backend is available on {system}"
        )
    return SandboxCommand(("/bin/sh", "-lc", command), "tree", False)


def sanitized_environment(
    extra: dict[str, str] | None = None,
    *,
    allowlist: Sequence[str] = (),
) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "CI",
        "PYTHONDONTWRITEBYTECODE",
        *allowlist,
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if extra:
        env.update(extra)
    return env
