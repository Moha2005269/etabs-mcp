"""Live backend: hosts .NET via pythonnet, loads the matching ETABSv1.dll and talks to ETABS.

Everything here runs on the session's single worker thread; ETABS is not thread-safe and COM proxies
are bound to the thread that created them.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .catalog import ApiCatalog, MethodSig
from .coerce import DEFAULT, PLACEHOLDER
from .discovery import (
    PROG_ID,
    EtabsInstall,
    RunningEtabs,
    find_installations,
    find_running_etabs,
    select_installation,
)
from .errors import (
    ConnectionLostError,
    EtabsCallFailed,
    InstallNotFoundError,
    NotConnectedError,
    RuntimeLoadError,
)
from .settings import Settings

log = logging.getLogger(__name__)

# Process-wide: one CLR, one ETABSv1 assembly. Two versions of the same assembly identity cannot be
# loaded side by side, so switching ETABS major versions needs a server restart.
_loaded_dll: Path | None = None
_module: Any = None

# HRESULTs that mean the ETABS process went away or the proxy is dead.
_DISCONNECT_HRESULTS = {0x80010108, 0x800706BA, 0x800706BE, 0x800401FD, 0x80010012}


def load_api(install: EtabsInstall, runtime: str) -> Any:
    global _loaded_dll, _module
    if _module is not None:
        if _loaded_dll != install.api_dll:
            raise RuntimeLoadError(
                f"The API of {_loaded_dll.parent.name if _loaded_dll else '?'} is already loaded in this server "
                f"process; it cannot also load {install.directory.name}.",
                hint="Restart the MCP server (set ETABS_MCP_VERSION or ETABS_MCP_INSTALL_DIR) to switch versions.",
            )
        return _module
    try:
        import pythonnet  # type: ignore[import-not-found]

        try:
            pythonnet.load(runtime)
        except RuntimeError:
            pass  # a runtime is already initialised in this process
        import clr  # type: ignore[import-not-found]

        clr.AddReference(str(install.api_dll))
        import ETABSv1  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on the host
        raise RuntimeLoadError(
            f"Could not load {install.api_dll} with the '{runtime}' .NET runtime: {exc}",
            hint="ETABS 22+ needs the .NET 8 desktop runtime (coreclr). Try ETABS_MCP_RUNTIME=netfx otherwise.",
        ) from exc
    _loaded_dll, _module = install.api_dll, ETABSv1
    return ETABSv1


# ------------------------------------------------------------------------- reflection


def _type_name(t: Any) -> str:
    if t.IsByRef:
        t = t.GetElementType()
    if t.IsArray:
        return _type_name(t.GetElementType()) + "[]"
    return str(t.Name)


def catalog_from_assembly(etabs_module: Any, version: str | None) -> ApiCatalog:
    """Reflect the loaded ETABSv1 assembly into the same spec shape tools/ApiDump produces."""
    import clr  # type: ignore[import-not-found]
    from System import Enum  # type: ignore[import-not-found]
    from System.Reflection import BindingFlags  # type: ignore[import-not-found]

    asm = clr.GetClrType(etabs_module.cSapModel).Assembly
    flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly
    interfaces: dict[str, Any] = {}
    enums: dict[str, dict[str, int]] = {}
    for t in asm.GetExportedTypes():
        name = str(t.Name)
        if t.IsEnum:
            enums[name] = {str(n): int(Enum.Parse(t, n)) for n in Enum.GetNames(t)}
            continue
        if not t.IsInterface:
            continue
        props = list(t.GetProperties(flags))
        accessors = {str(a.Name) for p in props for a in p.GetAccessors()}
        methods = []
        for m in t.GetMethods(flags):
            if m.IsSpecialName or str(m.Name) in accessors:
                continue
            params = []
            for p in m.GetParameters():
                has_default = p.HasDefaultValue and p.DefaultValue is not None
                params.append(
                    {
                        "name": str(p.Name),
                        "type": _type_name(p.ParameterType),
                        "by_ref": bool(p.ParameterType.IsByRef),
                        "out": bool(p.IsOut),
                        "optional": bool(p.IsOptional),
                        "default": str(p.DefaultValue) if has_default else None,
                    }
                )
            methods.append({"name": str(m.Name), "returns": _type_name(m.ReturnType), "params": params})
        interfaces[name] = {
            "extends": [str(i.Name) for i in t.GetInterfaces() if i.Assembly == asm],
            "properties": [{"name": str(p.Name), "type": _type_name(p.PropertyType)} for p in props],
            "methods": methods,
        }
    spec = {"interfaces": interfaces, "enums": enums, "file_version": version}
    return ApiCatalog.from_spec(spec, source=f"live ETABSv1.dll ({version})")


# ------------------------------------------------------------------------- marshalling


_ZERO = object()  # store the type's default value


def to_dotnet(value: Any, t: Any) -> Any:
    """Convert a coerced Python value to something assignable into a typed slot of type t.

    Arrays are built element by element through the typed indexer, which is where pythonnet applies
    the element type's conversion (a Python int becomes Int32, not a boxed PyInt)."""
    from System import Array, Enum  # type: ignore[import-not-found]

    if t.IsArray:
        et = t.GetElementType()
        arr = Array.CreateInstance(et, len(value))
        for i, v in enumerate(value):
            arr[i] = to_dotnet(v, et)
        return arr
    if t.IsEnum:
        return Enum.Parse(t, value, True)
    return value


def store(dest: Any, index: int, value: Any, t: Any) -> None:
    """Write value into dest (an object[]) at index, boxed as exactly type t.

    Assigning a Python int straight into object[] boxes a PyInt, which reflection rejects for Int32
    parameters. Going through a one-element t[] and Array.Copy yields a correctly typed box."""
    from System import Array  # type: ignore[import-not-found]

    tmp = Array.CreateInstance(t, 1)
    if value is not _ZERO:
        tmp[0] = value
    Array.Copy(tmp, 0, dest, index, 1)


def placeholder(t: Any) -> Any:
    from System import Array  # type: ignore[import-not-found]

    if t.IsArray:
        return Array.CreateInstance(t.GetElementType(), 0)
    if str(t.FullName) == "System.String":
        return ""
    return _ZERO


def to_python(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    from System import Array, Enum  # type: ignore[import-not-found]

    if isinstance(obj, Array):
        return [to_python(x) for x in obj]
    if isinstance(obj, Enum):
        return str(obj.ToString())
    try:
        return str(obj.ToString())
    except Exception:
        return repr(obj)


def _unwrap(exc: BaseException) -> BaseException:
    inner = getattr(exc, "InnerException", None)
    while inner is not None:
        exc = inner
        inner = getattr(exc, "InnerException", None)
    return exc


def is_connection_error(exc: BaseException) -> bool:
    root = _unwrap(exc)
    name = type(root).__name__
    if name in {"InvalidComObjectException", "RemotingException"}:
        return True
    hresult = getattr(root, "HResult", None)
    return hresult is not None and (int(hresult) & 0xFFFFFFFF) in _DISCONNECT_HRESULTS


# ------------------------------------------------------------------------- backend


class LiveBackend:
    """Backend that drives a real ETABS process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.module: Any = None
        self.oapi: Any = None
        self.sap_model: Any = None
        self.install: EtabsInstall | None = None
        self.pid: int | None = None
        self.started_by_server = False
        self.etabs_version: str | None = None
        self.catalog: ApiCatalog | None = None
        self._method_cache: dict[tuple[str, str, tuple[str, ...]], Any] = {}

    @property
    def connected(self) -> bool:
        return self.sap_model is not None

    # --- connection -------------------------------------------------------------------------

    def connect(
        self,
        *,
        mode: str,
        install_dir: Path | None,
        major: int | None,
        pid: int | None,
        visible: bool,
    ) -> dict[str, Any]:
        installs = find_installations(self.settings.extra_search_dirs)
        running = find_running_etabs()
        target: RunningEtabs | None = None

        if mode in ("attach", "auto"):
            candidates = running
            if pid is not None:
                candidates = [r for r in running if r.pid == pid]
                if not candidates:
                    raise NotConnectedError(
                        f"No ETABS window belongs to process {pid}.",
                        hint=f"Running ETABS processes: {[r.to_dict() for r in running] or 'none'}.",
                    )
            elif install_dir is not None or major is not None:
                wanted = select_installation(installs, directory=install_dir, major=major)
                candidates = [r for r in running if r.exe and r.exe.parent.resolve() == wanted.directory.resolve()]
            if candidates:
                target = candidates[-1]
            elif mode == "attach":
                raise NotConnectedError(
                    "No running ETABS instance was found to attach to.",
                    hint="Open ETABS first, or connect with mode='start' to launch it.",
                )

        if target is not None:
            install = select_installation(installs, exe=target.exe) if target.exe else select_installation(installs)
        else:
            install = select_installation(installs, directory=install_dir, major=major)

        etabs = load_api(install, self.settings.runtime)
        helper = etabs.cHelper(etabs.Helper())

        oapi = None
        if target is not None:
            try:
                oapi = helper.GetObjectProcess(PROG_ID, target.pid)
            except Exception as exc:
                log.info("GetObjectProcess failed (%s); falling back to GetObject", exc)
            if oapi is None:
                try:
                    oapi = helper.GetObject(PROG_ID)
                except Exception as exc:
                    raise NotConnectedError(
                        f"ETABS (pid {target.pid}) is running but refused the API connection: {exc}",
                        hint="Make sure ETABS finished starting and no modal dialog is open; then retry.",
                    ) from exc
            self.pid, self.started_by_server = target.pid, False
        else:
            before = {r.pid for r in running}
            try:
                oapi = helper.CreateObject(str(install.exe))
            except Exception as exc:
                raise NotConnectedError(f"Could not create an ETABS instance from {install.exe}: {exc}") from exc
            ret = oapi.ApplicationStart()
            if ret != 0:
                raise NotConnectedError(
                    f"ETABS failed to start (ApplicationStart returned {ret}).",
                    hint="Check the ETABS license, then retry.",
                )
            self.pid = self._new_pid(before, install)
            self.started_by_server = True
            if not visible:
                oapi.Hide()

        if oapi is None:
            raise NotConnectedError("The ETABS API returned no application object.")

        self.module, self.oapi, self.install = etabs, oapi, install
        self.sap_model = oapi.SapModel
        _, self.etabs_version, _ = self.sap_model.GetVersion("", 0.0)
        if self.catalog is None:
            self.catalog = catalog_from_assembly(etabs, install.version)
        return self.info()

    @staticmethod
    def _new_pid(before: set[int], install: EtabsInstall) -> int | None:
        for _ in range(20):
            for r in find_running_etabs():
                if r.pid not in before and (r.exe is None or r.exe.parent.resolve() == install.directory.resolve()):
                    return r.pid
            time.sleep(0.25)
        return None

    def info(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "etabs_version": self.etabs_version,
            "install": self.install.to_dict() if self.install else None,
            "pid": self.pid,
            "started_by_server": self.started_by_server,
            "api_catalog": self.catalog.source if self.catalog else None,
        }

    def disconnect(self, *, close_app: bool, save: bool) -> None:
        if self.oapi is not None and close_app:
            try:
                self.oapi.ApplicationExit(save)
            except Exception as exc:
                if not is_connection_error(exc):
                    raise
        self.oapi = self.sap_model = None
        self.pid = None
        self._method_cache.clear()
        try:
            from System import GC  # type: ignore[import-not-found]

            GC.Collect()
            GC.WaitForPendingFinalizers()
        except Exception:
            pass

    def is_alive(self) -> bool:
        if self.sap_model is None:
            return False
        try:
            self.sap_model.GetModelIsLocked()
            return True
        except Exception as exc:
            if is_connection_error(exc):
                return False
            raise

    # --- calls ------------------------------------------------------------------------------

    def root(self, name: str) -> Any:
        if self.sap_model is None:
            raise NotConnectedError("Not connected to ETABS.", hint="Call etabs_connect first.")
        return self.oapi if name == "App" else self.sap_model

    def get_property(self, obj: Any, name: str) -> Any:
        return getattr(obj, name)

    def _method_info(self, sig: MethodSig) -> Any:
        key = (sig.interface, sig.name, tuple(p.name for p in sig.params))
        mi = self._method_cache.get(key)
        if mi is None:
            import clr  # type: ignore[import-not-found]

            iface = clr.GetClrType(getattr(self.module, sig.interface))
            for cand in iface.GetMethods():
                if str(cand.Name) == sig.name and tuple(str(p.Name) for p in cand.GetParameters()) == key[2]:
                    mi = cand
                    break
            if mi is None:
                raise EtabsCallFailed(f"{sig.qualified_name} is not present in the loaded ETABSv1.dll.", path=sig.qualified_name)
            self._method_cache[key] = mi
        return mi

    def invoke(self, target: Any, sig: MethodSig, values: list[Any]) -> tuple[Any, dict[str, Any]]:
        from System import Array, DBNull, Enum, Object  # type: ignore[import-not-found]

        mi = self._method_info(sig)
        pinfos = list(mi.GetParameters())
        net_args = Array.CreateInstance(Object, len(pinfos))
        for i, (pinfo, value) in enumerate(zip(pinfos, values)):
            ptype = pinfo.ParameterType.GetElementType() if pinfo.ParameterType.IsByRef else pinfo.ParameterType
            if value is DEFAULT:
                dv = pinfo.DefaultValue
                if dv is None or isinstance(dv, type(DBNull.Value)):
                    converted = placeholder(ptype)
                elif ptype.IsEnum and isinstance(dv, int):
                    converted = Enum.ToObject(ptype, dv)
                else:
                    converted = dv
            elif value is PLACEHOLDER:
                converted = placeholder(ptype)
            else:
                converted = to_dotnet(value, ptype)
            store(net_args, i, converted, ptype)
        try:
            ret = mi.Invoke(target, net_args)
        except Exception as exc:
            if is_connection_error(exc):
                self.oapi = self.sap_model = None
                raise ConnectionLostError(
                    "The connection to ETABS was lost (the program was closed or crashed).",
                    hint="Call etabs_connect to attach again or start a new instance.",
                ) from exc
            root = _unwrap(exc)
            raise EtabsCallFailed(
                f"{sig.qualified_name} raised inside ETABS: {type(root).__name__}: {getattr(root, 'Message', root)}",
                path=sig.qualified_name,
            ) from exc
        outputs = {
            str(pinfo.Name): to_python(net_args[i])
            for i, pinfo in enumerate(pinfos)
            if pinfo.ParameterType.IsByRef
        }
        return to_python(ret), outputs


def require_install_available(settings: Settings) -> list[EtabsInstall]:
    installs = find_installations(settings.extra_search_dirs)
    if not installs:
        raise InstallNotFoundError("No ETABS installation found on this machine.")
    return installs
