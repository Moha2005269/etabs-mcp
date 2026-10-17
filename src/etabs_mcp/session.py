"""Session: owns the ETABS connection and serialises every API call onto one worker thread."""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from . import policy
from .catalog import ApiCatalog, MethodSig
from .coerce import build_result, choose_overload, coerce_arguments, jsonable
from .errors import ArgumentValidationError, BusyError, EtabsCallFailed, EtabsMcpError, NotConnectedError
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


@dataclass
class Job:
    """A long ETABS operation (analysis, design) running on the worker thread while the server stays responsive."""

    id: str
    kind: str
    description: str
    future: concurrent.futures.Future
    started: float = field(default_factory=time.time)
    finished: float | None = None
    log_path: Path | None = None

    @property
    def status(self) -> str:
        if not self.future.done():
            return "running"
        return "failed" if self.future.exception() else "done"

    def to_dict(self, *, log_lines: int = 12) -> dict[str, Any]:
        end = self.finished or time.time()
        out: dict[str, Any] = {"job_id": self.id, "kind": self.kind, "description": self.description,
                               "status": self.status, "elapsed_seconds": round(end - self.started, 1)}
        if self.status == "done":
            out["result"] = self.future.result()
        elif self.status == "failed":
            exc = self.future.exception()
            out["error"] = exc.to_dict() if isinstance(exc, EtabsMcpError) else {"error": type(exc).__name__,
                                                                                   "message": str(exc)}
        if self.log_path and log_lines and self.log_path.is_file():
            try:
                lines = self.log_path.read_text(errors="replace").splitlines()
                out["etabs_log_tail"] = [ln for ln in lines if ln.strip()][-log_lines:]
            except OSError:
                pass
        return out


class EtabsSession:
    def __init__(self, settings: Settings, backend: Backend | None = None,
                 backend_factory: Callable[[], Backend] | None = None):
        self.settings = settings
        if backend_factory is None:
            def backend_factory() -> Backend:
                from .dotnet import LiveBackend

                return LiveBackend(settings)
        self._factory = backend_factory
        # Several ETABS instances can be driven from one server; tools act on the active one.
        self._backends: dict[str, Backend] = {"default": backend or backend_factory()}
        self.active_instance = "default"
        self._audit_lock = threading.Lock()
        self._bundled: ApiCatalog | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="etabs-api")
        self._worker_ident: int | None = None
        self.jobs: dict[str, Job] = {}
        self._job_ids = itertools.count(1)

    # ---------------------------------------------------------------- instances

    @property
    def backend(self) -> Backend:
        return self._backends[self.active_instance]

    @property
    def instances(self) -> dict[str, Backend]:
        return dict(self._backends)

    def use_instance(self, name: str, *, create: bool = False) -> Backend:
        if name not in self._backends:
            if not create:
                raise NotConnectedError(f"No ETABS instance named '{name}'.",
                                        hint=f"Instances: {sorted(self._backends)}. Connect one with etabs_connect(instance=...).")
            self._backends[name] = self._factory()
        self.active_instance = name
        return self._backends[name]

    def drop_instance(self, name: str) -> None:
        if name == "default":
            raise ArgumentValidationError("The 'default' instance cannot be removed; disconnect it instead.")
        self._backends.pop(name, None)
        if self.active_instance == name:
            self.active_instance = "default"

    # ---------------------------------------------------------------- threading

    @property
    def active_job(self) -> Job | None:
        for job in reversed(list(self.jobs.values())):
            if job.status == "running":
                return job
        return None

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run fn on the ETABS worker thread. Calls are strictly serialised.

        While a background job owns ETABS, other calls fail fast with 'busy' instead of queueing silently."""
        job = self.active_job
        if job is not None:
            raise BusyError(
                f"ETABS is busy with background job {job.id} ({job.description}).",
                hint=f"Poll etabs_job_status(job_id='{job.id}') or wait with etabs_job_wait, then retry.",
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._enter, fn, args)

    def start_job(self, kind: str, description: str, fn: Callable[[], Any], *, log_path: Path | None = None) -> Job:
        """Queue fn on the worker thread and return immediately."""
        if self.active_job is not None:
            raise BusyError(f"Job {self.active_job.id} is still running.", hint="Wait for it with etabs_job_wait.")
        job_id = f"job-{next(self._job_ids)}"
        future = self._executor.submit(self._enter, fn, ())
        job = Job(id=job_id, kind=kind, description=description, future=future, log_path=log_path)

        def _finish(_: concurrent.futures.Future) -> None:
            job.finished = time.time()

        future.add_done_callback(_finish)
        self.jobs[job_id] = job
        return job

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
        try:
            ret, outputs = self.backend.invoke(target, sig, values)
        except EtabsMcpError as exc:
            self._audit(sig, resolved.path, args, None, exc.code)
            raise
        result = build_result(
            sig, resolved.path, ret, outputs, max_items=max_items or self.settings.max_array_items
        )
        self._audit(sig, resolved.path, args, result.get("ok", True), None)
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

    def _audit(self, sig: MethodSig, path: str, args: Any, ok: bool | None, error: str | None) -> None:
        """Append model-changing calls to the audit log (JSON lines) when one is configured."""
        if self.settings.audit_log is None or policy.classify(sig) == "read":
            return
        safe, _ = jsonable(args if isinstance(args, (dict, list)) else {}, max_items=50)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "instance": self.active_instance, "call": path,
                 "safety": policy.classify(sig), "args": safe, "ok": ok}
        if error:
            entry["error"] = error
        try:
            self.settings.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_lock, self.settings.audit_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            log.warning("audit log write failed: %s", exc)

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
