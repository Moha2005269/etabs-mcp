"""Find ETABS installations and running ETABS instances on this machine."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import InstallNotFoundError

MIN_SUPPORTED_MAJOR = 22
API_DLL = "ETABSv1.dll"
EXE = "ETABS.exe"
PROG_ID = "CSI.ETABS.API.ETABSObject"

_DIR_RE = re.compile(r"^ETABS\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class EtabsInstall:
    directory: Path
    major: int
    version: str

    @property
    def exe(self) -> Path:
        return self.directory / EXE

    @property
    def api_dll(self) -> Path:
        return self.directory / API_DLL

    @property
    def supported(self) -> bool:
        return self.major >= MIN_SUPPORTED_MAJOR

    def to_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "major": self.major,
            "version": self.version,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class RunningEtabs:
    pid: int
    exe: Path | None
    hwnd: int | None
    title: str

    def to_dict(self) -> dict:
        return {"pid": self.pid, "exe": str(self.exe) if self.exe else None, "title": self.title}


def file_version(path: Path) -> str | None:
    try:
        import win32api  # type: ignore[import-not-found]

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def _search_roots(extra: Iterable[Path]) -> list[Path]:
    roots = list(extra)
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Computers and Structures")
    seen, unique = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def inspect_install_dir(directory: Path) -> EtabsInstall | None:
    """Return an install record if `directory` holds an ETABS exe plus its API assembly."""
    if not (directory / EXE).is_file() or not (directory / API_DLL).is_file():
        return None
    version = file_version(directory / EXE)
    match = _DIR_RE.match(directory.name)
    if version:
        major = int(version.split(".")[0])
    elif match:
        major = int(match.group(1))
    else:
        return None
    return EtabsInstall(directory=directory, major=major, version=version or str(major))


def find_installations(extra_dirs: Iterable[Path] = ()) -> list[EtabsInstall]:
    """All ETABS installs found, newest first. Unsupported (< v22) installs are included but flagged."""
    found: dict[str, EtabsInstall] = {}
    extra = list(extra_dirs)
    for d in extra:
        inst = inspect_install_dir(d)
        if inst:
            found[str(inst.directory).lower()] = inst
    for root in _search_roots(extra):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and _DIR_RE.match(child.name):
                inst = inspect_install_dir(child)
                if inst:
                    found[str(inst.directory).lower()] = inst

    def sort_key(i: EtabsInstall) -> tuple:
        return tuple(int(p) if p.isdigit() else 0 for p in i.version.split("."))

    return sorted(found.values(), key=sort_key, reverse=True)


def select_installation(
    installs: list[EtabsInstall],
    *,
    directory: Path | None = None,
    major: int | None = None,
    exe: Path | None = None,
) -> EtabsInstall:
    """Pick one install: explicit directory > the exe of a running instance > major version > newest."""
    if directory is not None:
        inst = inspect_install_dir(Path(directory))
        if inst is None:
            raise InstallNotFoundError(
                f"{directory} does not contain {EXE} and {API_DLL}.",
                hint="Point ETABS_MCP_INSTALL_DIR (or the 'install_dir' argument) at the ETABS program folder.",
            )
        return _require_supported(inst)
    supported = [i for i in installs if i.supported]
    if exe is not None:
        for inst in installs:
            if inst.exe.resolve() == Path(exe).resolve():
                return _require_supported(inst)
        inst = inspect_install_dir(Path(exe).parent)
        if inst:
            return _require_supported(inst)
    if major is not None:
        for inst in supported:
            if inst.major == major:
                return inst
        raise InstallNotFoundError(
            f"ETABS {major} is not installed.",
            hint=f"Installed: {[i.version for i in installs] or 'none'}.",
        )
    if not supported:
        raise InstallNotFoundError(
            f"No supported ETABS installation (v{MIN_SUPPORTED_MAJOR}+) was found.",
            hint="Install ETABS 22 or newer, or set ETABS_MCP_INSTALL_DIR to its program folder.",
            details={"found": [i.to_dict() for i in installs]},
        )
    return supported[0]


def _require_supported(inst: EtabsInstall) -> EtabsInstall:
    if not inst.supported:
        raise InstallNotFoundError(
            f"ETABS {inst.version} is older than v{MIN_SUPPORTED_MAJOR}, which this server requires.",
        )
    return inst


def find_running_etabs() -> list[RunningEtabs]:
    """Running ETABS processes that own a visible top-level window, newest pid last."""
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]
    except ImportError:
        return []

    by_pid: dict[int, RunningEtabs] = {}

    def visit(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in by_pid and by_pid[pid].hwnd:
            return True
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                exe = Path(win32process.GetModuleFileNameEx(handle, 0))
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            return True
        if exe.name.lower() != EXE.lower():
            return True
        title = win32gui.GetWindowText(hwnd)
        # The main frame is the titled window; tool windows of the same process come and go.
        if title:
            by_pid[pid] = RunningEtabs(pid=pid, exe=exe, hwnd=hwnd, title=title)
        return True

    win32gui.EnumWindows(visit, None)
    return sorted(by_pid.values(), key=lambda r: r.pid)


def main_window_for_pid(pid: int) -> RunningEtabs | None:
    for proc in find_running_etabs():
        if proc.pid == pid:
            return proc
    return None
