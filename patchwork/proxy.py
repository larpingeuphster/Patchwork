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
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import rules
from .config import KIND_URL, Config
from .downloads import DownloadError, Downloads

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


def upstream_trust(app_dir: Path, extra_pems: tuple[str, ...] = ()) -> Path:
    """Write the certificates Patchwork trusts when it connects to Roblox's real servers.

    That's mitmproxy's usual list (certifi) plus everything Windows trusts. The
    Windows part matters when antivirus "HTTPS scanning" or a company network
    re-signs connections with its own certificate: Windows (and your browser)
    trusts that certificate, so Patchwork should too, or it can't reach Roblox.
    Verification itself stays on.
    """
    import ssl

    import certifi

    pems: list[str] = [Path(certifi.where()).read_text(encoding="ascii", errors="ignore")]
    added = 0
    if hasattr(ssl, "enum_certificates"):  # Windows only
        seen: set[bytes] = set()
        for store in ("ROOT", "CA"):
            try:
                certs = ssl.enum_certificates(store)
            except OSError:
                continue
            for der, encoding, _trust in certs:
                if encoding != "x509_asn" or der in seen:
                    continue
                seen.add(der)
                pems.append(ssl.DER_cert_to_PEM_cert(der))
                added += 1
    pems.extend(extra_pems)
    path = ca_dir(app_dir) / "upstream-trust.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(pems), encoding="ascii", errors="ignore")
    log.debug("Trusting certifi plus %d certificate(s) from Windows for upstream connections", added)
    return path


def downloads_dir(app_dir: Path) -> Path:
    return app_dir / "downloads"


def host_pattern(host: str) -> str:
    return rf"^{re.escape(host.strip().lower())}(:\d+)?$"


class SwapStats:
    """What Patchwork has seen from Roblox, for the Activity tab."""

    def __init__(self) -> None:
        self.trusted = 0  # TLS handshakes Roblox accepted
        self.rejected = 0  # TLS handshakes Roblox refused
        self.lookups = 0  # asset-delivery requests seen
        self.assets = 0  # individual assets asked for
        self.swapped = 0
        self.paused = 0  # requests seen while not in RIVALS
        self.upstream_failed = 0  # Patchwork couldn't verify Roblox's real server
        self._last_upstream_log = 0.0
        self.unknown_paths: set[str] = set()
        self._last_reject_log = 0.0


def _sni(data) -> str:
    for obj in (getattr(data, "conn", None), getattr(getattr(data, "context", None), "client", None)):
        sni = getattr(obj, "sni", None)
        if sni:
            return str(sni).lower()
    return ""


class SwapAddon:
    """mitmproxy addon that applies the swap table to asset-delivery traffic."""

    def __init__(
        self,
        config: Config,
        on_hit: Callable[[rules.Hit], None] | None = None,
        downloads_dir: Path | None = None,
        allow: Callable[[int | None], bool] | None = None,
    ):
        self.on_hit = on_hit or (lambda hit: None)
        # Decides whether swaps apply right now. Gets the place ID from the
        # request's Roblox-Place-Id header when Roblox sends one, else None.
        self.allow = allow
        self.downloads = Downloads(downloads_dir or Path(tempfile.gettempdir()) / "patchwork-downloads")
        self.stats = SwapStats()
        self.set_config(config)

    def set_config(self, config: Config) -> None:
        # Replaced atomically so a running proxy picks up edits immediately.
        self.table = rules.SwapTable(config.swaps)
        self.hosts = {h.strip().lower() for h in config.intercept_hosts}

    # --- TLS: tells us whether Roblox trusts Patchwork's certificate ---------------

    def tls_established_client(self, data) -> None:
        if _sni(data) in self.hosts:
            self.stats.trusted += 1
            if self.stats.trusted == 1:
                log.info("Roblox accepted Patchwork's certificate for %s. Swaps can apply.", _sni(data))

    def tls_failed_server(self, data) -> None:
        host = _sni(data) or str(getattr(getattr(getattr(data, "context", None), "server", None), "sni", "") or "")
        if host and host.lower() not in self.hosts:
            return
        self.stats.upstream_failed += 1
        now = time.monotonic()
        if now - self.stats._last_upstream_log > 30:
            self.stats._last_upstream_log = now
            log.warning(
                "Patchwork couldn't make a trusted connection to Roblox's real asset server (%d time(s)), so "
                "Roblox's requests can't go through. This usually means antivirus \"HTTPS scanning\" or a "
                "network filter is in the way: turn off HTTPS/web scanning in your antivirus, or add an "
                "exception for Patchwork, then press Stop and Start.", self.stats.upstream_failed)

    def tls_failed_client(self, data) -> None:
        host = _sni(data)
        if host not in self.hosts:
            return
        self.stats.rejected += 1
        now = time.monotonic()
        if now - self.stats._last_reject_log > 30:
            self.stats._last_reject_log = now
            log.warning(
                "Roblox refused Patchwork's certificate for %s (%d time(s) so far), so nothing can be swapped. "
                "Close Roblox completely, press Start, then open Roblox again. If it keeps happening, Roblox "
                "isn't using the certificate file Patchwork set up.", host, self.stats.rejected)

    def _note_unknown(self, method: str, path: str) -> None:
        shape = re.sub(r"\d{3,}", "<id>", path.split("?", 1)[0])
        if shape in self.stats.unknown_paths or len(self.stats.unknown_paths) >= 15:
            return
        self.stats.unknown_paths.add(shape)
        log.info("Roblox sent %s %s to the asset server. Patchwork doesn't handle this kind of request yet.",
                 method, shape)

    def _ours(self, flow) -> bool:
        return flow.request.pretty_host.lower() in self.hosts

    def _report(self, hits) -> None:
        for hit in hits:
            try:
                self.on_hit(hit)
            except Exception:  # never let the UI break the proxy
                log.exception("hit callback failed")

    async def prefetch(self) -> None:
        """Download every web-link swap ahead of time and report broken links."""
        for swap in self.table.url_swaps():
            try:
                await self.downloads.get(swap.value)
            except DownloadError as exc:
                log.warning("Couldn't download %s for %s: %s", swap.value, swap.name, exc)

    async def _serve(self, flow, swap) -> bool:
        """Answer ``flow`` with the replacement file for ``swap``. False if unavailable."""
        from mitmproxy import http

        if swap.kind == KIND_URL:
            try:
                path = await self.downloads.get(swap.value)
            except DownloadError as exc:
                log.warning("Couldn't download the replacement for %s: %s", swap.name, exc)
                return False
            ctype = rules.content_type_for(Path(swap.value.split("?", 1)[0]))
        else:
            path = Path(swap.value)
            if not path.is_file():
                log.warning("Replacement file missing for %s: %s", swap.name, path)
                return False
            ctype = rules.content_type_for(path)
        data = await asyncio.to_thread(path.read_bytes)
        flow.response = http.Response.make(200, data, {"Content-Type": ctype})
        return True

    async def request(self, flow) -> None:
        if not self._ours(flow):
            return
        from mitmproxy import http

        req = flow.request
        path = req.path

        if path.startswith(rules.FILE_PREFIX):
            token = path[len(rules.FILE_PREFIX) :].split("?", 1)[0]
            swap = self.table.served_for_token(token)
            if swap is None or not await self._serve(flow, swap):
                flow.response = http.Response.make(404, b"Patchwork: replacement not available")
            return

        self.stats.lookups += 1
        is_batch = req.method == "POST" and rules.is_batch_path(path)
        is_single = req.method == "GET" and rules.is_single_path(path)
        if not (is_batch or is_single):
            self._note_unknown(req.method, path)
            return

        if len(self.table) == 0:
            return

        if self.allow is not None:
            header = req.headers.get("Roblox-Place-Id", "")
            if not self.allow(int(header) if header.isdigit() else None):
                self.stats.paused += 1
                return

        if req.method == "POST" and rules.is_batch_path(path):
            try:
                items = json.loads(req.get_text() or "null")
            except (ValueError, UnicodeDecodeError):
                return
            if isinstance(items, list):
                self.stats.assets += len(items)
            changed, file_items, hits = rules.rewrite_batch_request(items, self.table)
            self.stats.swapped += len(hits)
            if changed:
                req.set_text(json.dumps(items))
            if file_items:
                flow.metadata["patchwork_files"] = file_items
            self._report(hits)
            return

        if req.method == "GET":
            self.stats.assets += 1
            decision = rules.decide_single(path, self.table)
            if decision is None:
                return
            self.stats.swapped += 1
            if decision.remove:
                flow.response = http.Response.make(404, b"Request asset was not found")
            elif decision.new_path is not None:
                req.path = decision.new_path
            elif decision.serve_file:
                if not await self._serve(flow, decision.swap):
                    return
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
        allow: Callable[[int | None], bool] | None = None,
        extra_upstream_pems: tuple[str, ...] = (),
    ):
        self.app_dir = app_dir
        self.config = config
        self.mode = mode  # tests use a regular proxy; the app uses local redirect mode
        self.extra_upstream_pems = extra_upstream_pems  # tests only
        self.addon = SwapAddon(config, on_hit, downloads_dir(app_dir), allow)
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
        if self.running:
            asyncio.run_coroutine_threadsafe(self.addon.prefetch(), self._loop)

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
            ssl_verify_upstream_trusted_ca=str(upstream_trust(self.app_dir, self.extra_upstream_pems)),
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
        self._prefetch_task = asyncio.create_task(self.addon.prefetch())

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
