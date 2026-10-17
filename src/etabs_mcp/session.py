"""Session: owns the ETABS connection and serialises every API call onto one worker thread."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from . import policy
from .catalog import ApiCatalog, MethodSig
from .coerce import build_result, choose_overload, coerce_arguments
from .errors import EtabsCallFailed, NotConnectedError
from .settings import Settings

log = logging.getLogger(__name__)
T = TypeVar("T")


class Backend(Protocol):
    catalog: ApiCatalog | None

    @property
    def connected(self) -> bool: ...

    def connect(
        self, *, mode: str, install_dir: Path | None, major: int | None, pid: int | None, visible: bool
    ) -> dict[str, Any]: ...

    def disconnect(self, *, close_app: bool, save: bool) -> None: ...

    def is_alive(self) -> bool: ...

    def info(self) -> dict[str, Any]: ...

    def root(self, name: str) -> Any: ...

    def get_property(self, obj: Any, name: str) -> Any: ...

    def invoke(self, target: Any, sig: MethodSig, values: list[Any]) -> tuple[Any, dict[str, Any]]: ...


class EtabsSession:
    def __init__(self, settings: Settings, backend: Backend | None = None):
        self.settings = settings
        if backend is None:
            from .dotnet import LiveBackend

            backend = LiveBackend(settings)
        self.backend = backend
        self._bundled: ApiCatalog | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="etabs-api")
        self._worker_ident: int | None = None

    # ---------------------------------------------------------------- threading

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run fn on the ETABS worker thread. Calls are strictly serialised."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._enter, fn, args)

    def _enter(self, fn: Callable[..., T], args: tuple) -> T:
        self._worker_ident = threading.get_ident()
        return fn(*args)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- catalog

    @property
    def catalog(self) -> ApiCatalog:
        if self.backend.catalog is not None:
            return self.backend.catalog
        if self._bundled is None:
            self._bundled = ApiCatalog.load_bundled()
        return self._bundled

    # ---------------------------------------------------------------- connection (worker thread)

    def connect_sync(
        self,
        *,
        mode: str | None = None,
        install_dir: Path | None = None,
        major: int | None = None,
        pid: int | None = None,
        visible: bool | None = None,
        reconnect: bool = False,
    ) -> dict[str, Any]:
        if self.backend.connected and not reconnect:
            if self.backend.is_alive():
                return self.backend.info()
            log.warning("ETABS connection is stale; reconnecting")
        if self.backend.connected:
            self.backend.disconnect(close_app=False, save=False)
        return self.backend.connect(
            mode=mode or self.settings.connect_mode,
            install_dir=install_dir if install_dir is not None else self.settings.install_dir,
            major=major if major is not None else self.settings.version,
            pid=pid,
            visible=self.settings.visible if visible is None else visible,
        )

    def ensure_connected(self) -> None:
        if not self.backend.connected:
            self.connect_sync()

    def disconnect_sync(self, *, close_app: bool = False, save: bool = False) -> None:
        if self.backend.connected:
            self.backend.disconnect(close_app=close_app, save=save)

    # ---------------------------------------------------------------- calls (worker thread)

    def call(
        self,
        path: str,
        args: Any = None,
        *,
        confirm: bool = False,
        check: bool = True,
        max_items: int | None = None,
        _internal: bool = False,
    ) -> dict[str, Any]:
        """Resolve, validate and invoke one API method. With check=True a non-zero status raises."""
        self.ensure_connected()
        catalog = self.catalog
        resolved = catalog.resolve(path)
        sig = choose_overload(resolved.methods, args)
        if not _internal:
            policy.check(
                sig,
                read_only=self.settings.read_only,
                allow_destructive=self.settings.allow_destructive,
                confirm=confirm,
            )
        elif self.settings.read_only and policy.classify(sig) != "read":
            policy.check(sig, read_only=True, allow_destructive=True, confirm=True)
        values = coerce_arguments(sig, args, catalog)

        target = self.backend.root(resolved.root)
        for prop in resolved.chain:
            target = self.backend.get_property(target, prop)
        ret, outputs = self.backend.invoke(target, sig, values)
        result = build_result(
            sig, resolved.path, ret, outputs, max_items=max_items or self.settings.max_array_items
        )
        if check and result.get("ok") is False:
            raise EtabsCallFailed(
                f"ETABS returned status {ret} from {resolved.path}.",
                path=resolved.path,
                ret=ret,
                hint=self.failure_hint(sig),
            )
        return result

    def out(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Internal helper for curated tools: call and return outputs merged with the return value."""
        res = self.call(path, kwargs, _internal=True, max_items=10**9)
        merged = dict(res.get("outputs", {}))
        if "value" in res:
            merged["_value"] = res["value"]
        return merged

    def value(self, path: str, **kwargs: Any) -> Any:
        return self.call(path, kwargs, _internal=True, max_items=10**9).get("value")

    def failure_hint(self, sig: MethodSig) -> str:
        hints = []
        if policy.classify(sig) != "read":
            try:
                locked = self.value("GetModelIsLocked")
            except Exception:
                locked = None
            if locked:
                hints.append(
                    "The model is locked because analysis results exist; edits fail until it is unlocked "
                    "(etabs_unlock_model, which deletes results)."
                )
        hints.append(
            "ETABS reports failures only as a status code. Check that names (objects, sections, patterns, "
            "cases) exist and that values are in the present units; etabs_describe_api shows the signature."
        )
        return " ".join(hints)

    def assert_connected(self) -> None:
        if not self.backend.connected:
            raise NotConnectedError("Not connected to ETABS.", hint="Call etabs_connect.")
