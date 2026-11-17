"""The local proxy: mitmproxy in local-redirect mode, limited to Roblox.

Local redirect mode captures only the traffic of the named processes
(RobloxPlayerBeta.exe by default), so nothing else on the PC is touched and no
system proxy setting changes. Of that traffic, only connections to the hosts in
``intercept_hosts`` are decrypted; everything else passes through untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

from . import rules
from .config import Config

log = logging.getLogger("patchwork")

CA_BASENAME = "mitmproxy"


def ca_dir(app_dir: Path) -> Path:
    return app_dir / "certs"


def ensure_ca(app_dir: Path) -> str:
    """Create this PC's certificate authority if needed and return its PEM.

    The key is generated locally on first run and never leaves this folder.
    """
    from mitmproxy.certs import CertStore

    directory = ca_dir(app_dir)
    directory.mkdir(parents=True, exist_ok=True)
    CertStore.from_store(directory, CA_BASENAME, 2048)
    return (directory / f"{CA_BASENAME}-ca-cert.pem").read_text(encoding="ascii")


def host_pattern(host: str) -> str:
    return rf"^{re.escape(host.strip().lower())}(:\d+)?$"


class SwapAddon:
    """mitmproxy addon that applies the swap table to asset-delivery traffic."""

    def __init__(self, config: Config, on_hit: Callable[[rules.Hit], None] | None = None):
        self.on_hit = on_hit or (lambda hit: None)
        self.set_config(config)

    def set_config(self, config: Config) -> None:
        # Replaced atomically so a running proxy picks up edits immediately.
        self.table = rules.SwapTable(config.swaps)
        self.hosts = {h.strip().lower() for h in config.intercept_hosts}

    def _ours(self, flow) -> bool:
        return flow.request.pretty_host.lower() in self.hosts

    def _report(self, hits) -> None:
        for hit in hits:
            try:
                self.on_hit(hit)
            except Exception:  # never let the UI break the proxy
                log.exception("hit callback failed")

    def request(self, flow) -> None:
        if not self._ours(flow):
            return
        from mitmproxy import http

        req = flow.request
        path = req.path

        if path.startswith(rules.FILE_PREFIX):
            token = path[len(rules.FILE_PREFIX) :].split("?", 1)[0]
            file = self.table.file_for_token(token)
            if file is None or not file.is_file():
                flow.response = http.Response.make(404, b"Patchwork: replacement file not found")
                log.warning("Replacement file missing: %s", file)
                return
            flow.response = http.Response.make(
                200, file.read_bytes(), {"Content-Type": rules.content_type_for(file)}
            )
            return

        if len(self.table) == 0:
            return

        if req.method == "POST" and rules.is_batch_path(path):
            try:
                items = json.loads(req.get_text() or "null")
            except (ValueError, UnicodeDecodeError):
                return
            changed, file_items, hits = rules.rewrite_batch_request(items, self.table)
            if changed:
                req.set_text(json.dumps(items))
            if file_items:
                flow.metadata["patchwork_files"] = file_items
            self._report(hits)
            return

        if req.method == "GET":
            decision = rules.decide_single(path, self.table)
            if decision is None:
                return
            if decision.new_path is not None:
                req.path = decision.new_path
            elif decision.serve_file:
                file = Path(decision.swap.value)
                if not file.is_file():
                    log.warning("Replacement file missing: %s", file)
                    return
                flow.response = http.Response.make(
                    200, file.read_bytes(), {"Content-Type": rules.content_type_for(file)}
                )
            elif decision.rewrite_json:
                flow.metadata["patchwork_single_file"] = decision.swap.value
            self._report([decision.hit])

    def response(self, flow) -> None:
        file_items = flow.metadata.get("patchwork_files")
        single = flow.metadata.get("patchwork_single_file")
        if not (file_items or single) or flow.response is None:
            return
        try:
            body = json.loads(flow.response.get_text() or "null")
        except (ValueError, UnicodeDecodeError):
            return
        host = flow.request.pretty_host
        if file_items:
            changed = rules.rewrite_batch_response(body, file_items, host)
        else:
            changed = rules.rewrite_json_response(body, host, single)
        if changed:
            if flow.response.status_code != 200:
                flow.response.status_code = 200
            flow.response.set_text(json.dumps(body))


class _UiLogHandler(logging.Handler):
    def __init__(self, sink: Callable[[str, str], None]):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        # Patchwork's own messages at INFO and up; mitmproxy's only when they matter.
        if record.levelno < logging.WARNING and not record.name.startswith("patchwork"):
            return
        try:
            self.sink(record.levelname, record.getMessage())
        except Exception:
            pass


class _Lifecycle:
    """Tells the runner when mitmproxy has finished bringing up the redirector."""

    def __init__(self, on_running: Callable[[], None]):
        self.on_running = on_running

    def running(self) -> None:
        self.on_running()


class ProxyRunner:
    """Runs mitmproxy on one background event loop so the GUI stays responsive.

    The loop lives for the whole app session: mitmproxy keeps its elevated
    redirector helper alive between starts (to avoid repeat UAC prompts), and
    that helper is tied to the loop that created it.
    """

    def __init__(
        self,
        config: Config,
        app_dir: Path,
        on_hit: Callable[[rules.Hit], None],
        on_log: Callable[[str, str], None],
        mode: str | None = None,
    ):
        self.app_dir = app_dir
        self.config = config
        self.mode = mode  # tests use a regular proxy; the app uses local redirect mode
        self.addon = SwapAddon(config, on_hit)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="patchwork-proxy", daemon=True)
        self._thread.start()
        self._master = None
        self._run_task: asyncio.Task | None = None
        self._errors: list[str] = []
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(_UiLogHandler(on_log))

        runner = self

        class _Errors(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                runner._errors.append(record.getMessage())

        root.addHandler(_Errors(logging.ERROR))

    @property
    def running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def update_config(self, config: Config) -> None:
        self.config = config
        self.addon.set_config(config)

    def start(self, timeout: float = 180.0) -> None:
        """Start intercepting. Raises RuntimeError with a readable reason on failure.

        The first start shows a Windows UAC prompt for mitmproxy's redirector
        helper, which is why the timeout is generous.
        """
        if self.running:
            return
        future = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        try:
            future.result(timeout)
        except TimeoutError:
            self.stop()
            raise RuntimeError("The proxy did not start in time. Did you accept the Windows permission prompt?")

    def stop(self) -> None:
        if self._master is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._stop(), self._loop)
        try:
            future.result(15)
        except Exception:
            log.exception("stopping the proxy failed")

    async def _start(self) -> None:
        from mitmproxy import addons, master, options

        self._errors.clear()
        started = asyncio.Event()
        processes = ",".join(self.config.processes)
        opts = options.Options(
            mode=[self.mode or f"local:{processes}"],
            confdir=str(ca_dir(self.app_dir)),
            allow_hosts=[host_pattern(h) for h in self.config.intercept_hosts],
        )
        m = master.Master(opts)
        m.addons.add(*addons.default_addons())
        m.addons.add(self.addon, _Lifecycle(started.set))
        self._master = m
        self._run_task = asyncio.create_task(m.run())

        waiter = asyncio.create_task(started.wait())
        await asyncio.wait({waiter, self._run_task}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        await asyncio.sleep(0)  # let pending log records arrive

        if self._errors or self._run_task.done():
            reason = self._errors[0] if self._errors else "The proxy stopped during startup."
            await self._stop()
            raise RuntimeError(reason)

    async def _stop(self) -> None:
        m, task = self._master, self._run_task
        self._master = None
        if m is None:
            return
        # Master.shutdown() leaves listeners and the redirector's capture rules in
        # place, so stop the servers explicitly; that also releases Roblox's traffic.
        ps = m.addons.get("proxyserver")
        if ps is not None:
            try:
                await asyncio.wait_for(ps.servers.update([]), 10)
            except Exception:
                log.exception("stopping proxy servers failed")
        m.shutdown()
        if task is not None:
            try:
                await asyncio.wait_for(task, 10)
            except Exception:
                pass
        self._run_task = None
