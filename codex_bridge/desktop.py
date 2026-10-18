"""Opt-in Codex Desktop adapter for the app's local native-tools pipe.

This is a version-dependent companion interface, not a public desktop API.
Only list/read and explicitly requested sends are exposed. No UI automation,
approval emulation, security setting changes, or retries of ambiguous sends.
"""
from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path


class DesktopError(RuntimeError):
    def __init__(self, reason, detail=""):
        self.reason, self.detail = reason, detail
        super().__init__(detail or reason)


MAX_FRAME = 8 * 1024 * 1024
TOOLS = {"list_threads", "read_thread", "send_message_to_thread"}


class _WindowsPipe:
    """Byte-mode named pipe, with bounded nonblocking reads."""
    def __init__(self, path):
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        self.api.CreateFileW.restype = w.HANDLE
        self.api.SetNamedPipeHandleState.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD), w.LPVOID, w.LPVOID]
        self.api.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
        self.api.WriteFile.argtypes = self.api.ReadFile.argtypes
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise DesktopError("desktop_unavailable", "Codex 桌面本地接口不可用")
        mode = w.DWORD(1)  # PIPE_NOWAIT, PIPE_READMODE_BYTE
        if not self.api.SetNamedPipeHandleState(self.handle, ctypes.byref(mode), None, None):
            self.close()
            raise DesktopError("desktop_unavailable")

    def write(self, data):
        from ctypes import wintypes as w
        count = w.DWORD()
        if not self.api.WriteFile(self.handle, data, len(data), ctypes.byref(count), None) or count.value != len(data):
            raise DesktopError("unfinished", "桌面发送结果未知；请先检查聊天，勿重复发送")

    def read(self):
        from ctypes import wintypes as w
        buffer, count = ctypes.create_string_buffer(65536), w.DWORD()
        if self.api.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
            return buffer.raw[:count.value]
        if ctypes.get_last_error() == 232:  # ERROR_NO_DATA
            return b""
        raise DesktopError("desktop_disconnected", "Codex 桌面连接已断开")

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def seed_thread_id():
    """Read-only caller context. A discovered chat is never selected for sending."""
    value = os.environ.get("CODEX_THREAD_ID", "")
    if value:
        return value
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for path in sorted(home.glob("state_*.sqlite"), reverse=True):
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                row = db.execute("SELECT id FROM threads WHERE archived=0 AND originator='Codex Desktop' "
                                 "ORDER BY updated_at DESC LIMIT 1").fetchone()
                if row:
                    return row[0]
        except sqlite3.Error:
            continue
    raise DesktopError("desktop_no_chat", "请先在 Codex 桌面打开一个本地聊天")


class NativeDesktopClient:
    def __init__(self, pipe_factory=None, candidates=None, caller_id=""):
        self._factory = pipe_factory or _WindowsPipe
        self._candidates = candidates
        self.caller_id = caller_id
        self.pipe_path = ""
        self._lock = threading.RLock()

    @staticmethod
    def candidates():
        if os.name != "nt":
            raise DesktopError("desktop_unsupported", "桌面接入目前支持 Windows")
        explicit = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
        if explicit:
            return [explicit]
        try:
            return ["\\\\.\\pipe\\" + name for name in os.listdir("\\\\.\\pipe\\")
                    if name.startswith("codex-browser-use-")]
        except OSError as exc:
            raise DesktopError("desktop_unavailable") from exc

    def _rpc(self, path, method, params, timeout=20):
        rid = "control-" + uuid.uuid4().hex
        payload = json.dumps({"id": rid, "jsonrpc": "2.0", "method": method, "params": params},
                             ensure_ascii=False).encode("utf-8")
        if len(payload) > 60000:
            raise DesktopError("too_long", "指令过长")
        pipe = self._factory(path)
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        try:
            pipe.write(struct.pack("<I", len(payload)) + payload)
            while time.monotonic() < deadline:
                buffer.extend(pipe.read())
                while len(buffer) >= 4:
                    size = struct.unpack_from("<I", buffer)[0]
                    if size > MAX_FRAME:
                        raise DesktopError("desktop_protocol", "桌面接口响应过大")
                    if len(buffer) < size + 4:
                        break
                    message = json.loads(buffer[4:size + 4])
                    del buffer[:size + 4]
                    if message.get("id") != rid:
                        continue
                    if message.get("error"):
                        raise DesktopError("desktop_api", str(message["error"].get("message", "接口调用失败")))
                    return message.get("result") or {}
                time.sleep(0.02)
            raise DesktopError("unfinished" if method == "tools/call" and params.get("tool") == "send_message_to_thread"
                               else "desktop_timeout", "桌面接口响应超时；发送操作不会自动重试")
        finally:
            pipe.close()

    def connect(self):
        with self._lock:
            paths = self._candidates() if self._candidates else self.candidates()
            matches = []
            for path in paths:
                try:
                    result = self._rpc(path, "tools/list", {"threadStartKind": "all"}, timeout=0.8)
                    names = {t.get("name") for t in result.get("tools", []) if t.get("namespace") == "codex_app"}
                    if TOOLS.issubset(names):
                        matches.append(path)
                except (DesktopError, OSError, ValueError):
                    continue
            if len(matches) != 1:
                self.pipe_path = ""
                raise DesktopError("desktop_ambiguous" if matches else "desktop_unavailable",
                                   "检测到多个桌面接口，请指定 CODEX_APP_TOOLS_PIPE_PATH" if matches
                                   else "请打开 Codex 桌面客户端并启用 Codex app tools 插件")
            self.pipe_path = matches[0]

    def call(self, tool, arguments, caller_id=""):
        if tool not in TOOLS:
            raise DesktopError("desktop_unsupported")
        with self._lock:
            if not self.pipe_path:
                self.connect()
            caller = caller_id or self.caller_id or seed_thread_id()
            self.caller_id = caller
            call_id = "control-" + uuid.uuid4().hex
            try:
                result = self._rpc(self.pipe_path, "tools/call", {
                    "arguments": arguments, "callerSource": "codex", "callId": call_id,
                    "hostId": "local", "namespace": "codex_app", "threadId": caller,
                    "tool": tool, "turnId": call_id})
            except (DesktopError, OSError):
                self.pipe_path = ""
                raise
            if result.get("success") is False:
                raise DesktopError("desktop_api", "桌面接口拒绝操作")
            for item in result.get("contentItems", []):
                if isinstance(item.get("text"), str):
                    try:
                        return json.loads(item["text"])
                    except ValueError:
                        if tool == "send_message_to_thread":
                            raise DesktopError("unfinished", item["text"][:200])
            raise DesktopError("desktop_protocol", "桌面接口响应格式不兼容")

    def list_chats(self):
        result = self.call("list_threads", {"limit": 50})
        seen, chats = set(), []
        for thread in result.get("pinnedThreads", []) + result.get("threads", []):
            if thread.get("kind") == "codex" and thread.get("hostId") == "local" and thread.get("id") not in seen:
                chats.append(thread)
                seen.add(thread["id"])
        return chats

    def read(self, tid):
        result = self.call("read_thread", {"threadId": tid, "hostId": "local", "turnLimit": 1,
                          "includeOutputs": False, "maxOutputCharsPerItem": 2000}, caller_id=tid)
        thread = result.get("thread") or {}
        if thread.get("id") != tid or thread.get("kind") != "codex" or thread.get("hostId") != "local":
            raise DesktopError("desktop_wrong_target", "目标聊天与绑定不一致")
        return result

    def send(self, tid, text, guard=None):
        if not text.strip():
            raise DesktopError("no_lines")
        # Fresh state immediately before the mutation; no retries of tools/call.
        with self._lock:
            snapshot = self.read(tid)
            kind, flags = desktop_status(snapshot)
            if kind != "idle" or flags:
                raise DesktopError("busy" if kind == "active" and not flags else "asking",
                                   "桌面聊天正在运行或需要你处理；请在桌面查看")
            if guard is not None and not guard():
                raise DesktopError("cancelled", "自动续跑已停止")
            return self.call("send_message_to_thread", {"threadId": tid, "hostId": "local", "prompt": text}, caller_id=tid)


def desktop_status(snapshot):
    status = (snapshot.get("thread") or {}).get("status") or {}
    if isinstance(status, str):
        return status, []
    return status.get("type", "unknown"), status.get("activeFlags") or []


def desktop_last(snapshot):
    turns = snapshot.get("turns") or []
    if not turns:
        return ""
    for item in reversed(turns[0].get("items") or []):
        if item.get("type") == "agentMessage":
            return str(item.get("text") or "")
    return ""


class ContinuePolicy:
    """Finite, opt-in continuation after an observed active -> completed turn."""
    def __init__(self):
        self.previous = {}
        self.spent = set()

    def ready(self, tid, snapshot, loops):
        kind, flags = desktop_status(snapshot)
        turns = snapshot.get("turns") or []
        turn = turns[0] if turns else {}
        turn_id = turn.get("id")
        observed = self.previous.get(tid)
        current = (turn_id, kind)
        if kind == "active" and not flags:
            self.previous[tid] = current
            return False
        if (kind == "idle" and not flags and loops > 0 and turn_id and observed == (turn_id, "active")
                and turn.get("status") == "completed" and not turn.get("error") and (tid, turn_id) not in self.spent):
            return True
        # Approvals, errors and unknown states never become implicit consent.
        self.previous[tid] = current
        return False

    def claim(self, tid, snapshot):
        turn = (snapshot.get("turns") or [{}])[0]
        self.spent.add((tid, turn.get("id")))
        self.previous[tid] = (turn.get("id"), "claimed")
