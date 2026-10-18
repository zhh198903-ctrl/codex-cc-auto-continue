"""Newline JSON-RPC to Codex CLI, with a dedicated reader and no shell execution."""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
from concurrent.futures import Future, TimeoutError
from pathlib import Path


class CodexError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail


def cli_command(executable: str = "") -> list[str]:
    """Resolve npm's Windows shim through Node so paths never pass through cmd.exe."""
    found = executable or shutil.which("codex") or ""
    if not found:
        raise CodexError("not_installed", "未找到 Codex CLI；请安装并运行 codex login")
    path = Path(found)
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        package = script.parent.parent
        platform_package = "codex-win32-arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "codex-win32-x64"
        for root in (package / "node_modules" / "@openai" / platform_package,
                     path.parent / "node_modules" / "@openai" / platform_package, package):
            native = list((root / "vendor").glob("*/bin/codex.exe")) or list((root / "vendor").glob("*/codex/codex.exe"))
            if native:
                return [str(native[0])]
        node = shutil.which("node")
        if not node or not script.is_file():
            raise CodexError("not_installed", "请指定 codex.exe，或安装 npm 版 Codex 与 Node.js")
        return [node, str(script)]
    return [str(path)]


class RpcClient:
    def __init__(self, log=None):
        self.log = log or (lambda _: None)
        self.events: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._pending: dict[int, Future] = {}
        self._serial = 0
        self.process = None
        self._ws = None
        self._ws_alive = False
        self._readers = []

    def _thread(self, target, transport, name):
        thread = threading.Thread(target=target, args=(transport,), daemon=True, name=name)
        self._readers.append(thread)
        thread.start()

    @property
    def connected(self) -> bool:
        return self._ws_alive or (self.process is not None and self.process.poll() is None)

    def _initialize(self):
        self.request("initialize", {"clientInfo": {"name": "claude_codex_control",
                     "title": "Codex Claude Control", "version": "0.3.0"},
                     "capabilities": {"experimentalApi": True}})
        self.notify("initialized")

    def start_shared(self):
        """The CLI daemon control socket speaks WebSocket, not JSONL (including its proxy)."""
        if self.connected:
            return
        from websockets.sync.client import connect
        path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "app-server-control" / "app-server-control.sock"
        if not path.exists():
            raise CodexError("no_daemon", "没有运行中的共享 Codex CLI；可先打开 codex，或点击「新建 Codex 会话」")
        sock = None
        try:
            if os.name == "nt":
                # CPython on Windows doesn't expose AF_UNIX or its address converter.
                # Windows 10+ Winsock supports it; hand connect() the native sockaddr_un.
                import ctypes
                encoded = str(path).encode("utf-8")
                if len(encoded) >= 108:
                    raise CodexError("socket_path", "Codex 控制套接字路径过长")
                class Address(ctypes.Structure):
                    _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * 108)]
                sock = socket.socket(1, socket.SOCK_STREAM)
                address = Address(1, encoded)
                native = ctypes.windll.ws2_32.connect
                native.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
                native.restype = ctypes.c_int
                if native(sock.fileno(), ctypes.byref(address), ctypes.sizeof(address)):
                    raise CodexError("no_daemon", "无法连接本机 Codex 服务；请打开 Codex CLI 或新建会话")
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(str(path))
            self._ws = connect("ws://localhost", sock=sock, open_timeout=5, close_timeout=2)
            self._ws_alive = True
            ws = self._ws
            self._thread(self._read_ws, ws, "codex-shared-rpc")
            self._initialize()
        except Exception as exc:
            self.close()
            if sock is not None:
                sock.close()
            if isinstance(exc, CodexError):
                raise
            raise CodexError("no_daemon", f"无法连接本机 Codex 服务：{exc}") from exc

    def start(self, command: list[str]) -> None:
        if self.connected:
            return
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                            errors="replace", bufsize=1,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise CodexError("start_failed", str(exc)) from exc
        process = self.process
        self._thread(self._read, process, "codex-rpc")
        self._thread(self._stderr, process, "codex-stderr")
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _write(self, message: dict) -> None:
        with self._lock:
            if not self.connected:
                raise CodexError("disconnected", "Codex 连接已断开")
            try:
                encoded = json.dumps(message, ensure_ascii=False)
                if self._ws_alive:
                    self._ws.send(encoded)
                else:
                    self.process.stdin.write(encoded + "\n")
                    self.process.stdin.flush()
            except Exception as exc:
                raise CodexError("disconnected", str(exc)) from exc

    def request(self, method: str, params=None, timeout: float = 20) -> dict:
        with self._lock:
            self._serial += 1
            rid = self._serial
            future = self._pending[rid] = Future()
        try:
            self._write({"id": rid, "method": method, "params": params or {}})
            return future.result(timeout)
        except TimeoutError as exc:
            # A write request may have executed. Never retry it automatically.
            raise CodexError("timeout", f"Codex {method} 超时；请查看会话后再操作") from exc
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def notify(self, method: str, params=None) -> None:
        self._write({"method": method, "params": params or {}})

    def respond(self, request_id, result: dict) -> None:
        self._write({"id": request_id, "result": result})

    def _read(self, process) -> None:
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._dispatch(message)
        finally:
            with self._lock:
                if self.process is process:
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(CodexError("disconnected", "Codex 连接已断开"))
                    self.events.put({"method": "control/disconnected", "params": {}})

    def _dispatch(self, message):
        if not isinstance(message, dict):
            return
        if "method" in message:
            self.events.put(message)
        elif "id" in message:
            with self._lock:
                future = self._pending.get(message["id"])
                if future is not None and not future.done():
                    if "error" in message:
                        err = message["error"]
                        future.set_exception(CodexError("rpc_error", str(err.get("message", err))))
                    else:
                        future.set_result(message.get("result") or {})

    def _read_ws(self, ws):
        try:
            for raw in ws:
                try:
                    self._dispatch(json.loads(raw))
                except ValueError:
                    continue
        except Exception:
            pass  # Connection closure is reported below; never print a daemon-thread traceback.
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws_alive = False
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(CodexError("disconnected"))
                    self.events.put({"method": "control/disconnected", "params": {}})

    def _stderr(self, process) -> None:
        # Do not mirror stderr: it can contain tool arguments and user context.
        try:
            for _ in process.stderr:
                pass
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws_alive = False
            for reader in self._readers:
                if reader is not threading.current_thread():
                    reader.join(timeout=2)
            self._readers.clear()
            self._ws = None
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for reader in self._readers:
            if reader is not threading.current_thread():
                reader.join(timeout=2)
        self._readers.clear()
