"""Codex sessions and approvals, using CLI app-server rather than terminal keystrokes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import queue
import secrets
import threading
import time
from pathlib import Path

from codex_bridge.client import CodexError, RpcClient, cli_command
from codex_bridge.types import Ask, Prompt, clean_text, IndexRow, clip

APPROVALS = {"item/commandExecution/requestApproval": "命令授权",
             "item/fileChange/requestApproval": "文件修改授权",
             "item/permissions/requestApproval": "访问权限授权"}
INPUT = "item/tool/requestUserInput"


def normalize_rate_limits(payload, now=None):
    """Quota windows are identified by duration, never by primary/secondary order."""
    windows = {'five_hour': None, 'weekly': None}
    if not isinstance(payload, dict):
        payload = {}
    buckets = payload.get('rateLimitsByLimitId')
    bucket = buckets.get('codex') if isinstance(buckets, dict) else payload.get('rateLimits')
    if isinstance(bucket, dict) and bucket.get('limitId', 'codex') == 'codex':
        for name in ('primary', 'secondary'):
            item = bucket.get(name)
            if not isinstance(item, dict):
                continue
            duration, used, reset = (item.get(k) for k in ('windowDurationMins', 'usedPercent', 'resetsAt'))
            key = {300: 'five_hour', 10080: 'weekly'}.get(duration) if type(duration) is int else None
            if (key and type(used) in (int, float) and math.isfinite(used)
                    and type(reset) is int and reset > 0):
                windows[key] = {'remaining_percent': round(100 - min(100, max(0, used)), 1),
                                'resets_at': reset, 'window_minutes': duration}
    return {'ok': any(windows.values()), 'provider': 'codex', 'updated_at': int(time.time() if now is None else now),
            **windows}


def session_id(thread_id: str) -> str:
    return "codex:" + thread_id


def session_key(sid: str) -> str:
    # UUIDv7 prefixes are timestamps: first eight characters are NOT unique.
    return "cx" + hashlib.sha256(sid.encode()).hexdigest()[:12]


class CodexBackend:
    def __init__(self, rpc=None, log=None):
        self.log = log or (lambda _: None)
        self.rpc = rpc or RpcClient(self.log)
        self.lock = threading.RLock()
        self._actions = threading.Lock()
        self.threads: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.shared = False
        self.error = ""
        self._stop = threading.Event()
        self._worker = None

    @property
    def connected(self):
        return self.rpc.connected

    def rate_limits(self):
        if self.connected:
            return normalize_rate_limits(self.rpc.request('account/rateLimits/read', timeout=10))
        # Native-only Control still needs account metadata. This temporary
        # reader never starts/resumes a thread, refreshes login, or runs a turn.
        reader = RpcClient()
        try:
            reader.start(cli_command() + ['app-server'])
            return normalize_rate_limits(reader.request('account/rateLimits/read', timeout=10))
        finally:
            reader.close()

    def connect(self, shared: bool = True, executable: str = ""):
        if self.connected:
            return
        self._stop.clear()
        self.shared = shared
        if shared:
            self.rpc.start_shared()
        else:
            self.rpc.start(cli_command(executable) + ["app-server"])
        self.error = ""
        self._worker = threading.Thread(target=self._run, daemon=True, name="codex-sessions")
        self._worker.start()
        self.log("Codex：已连接本机 CLI" if shared else "Codex：已启动独立 CLI 后端")

    def _run(self):
        refresh_at = 0.0
        while not self._stop.is_set() and self.connected:
            if self.shared and time.monotonic() >= refresh_at:
                try:
                    self.refresh()
                    self.error = ""
                except CodexError as exc:
                    self.error = exc.detail or exc.reason
                refresh_at = time.monotonic() + 5
            try:
                self.ingest(self.rpc.events.get(timeout=0.2))
            except queue.Empty:
                pass
            except Exception as exc:
                self.log(f"Codex event error: {type(exc).__name__}")
        self.ingest({"method": "control/disconnected", "params": {}})

    def refresh(self):
        loaded, cursor = set(), None
        while True:
            result = self.rpc.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            loaded.update(result.get("data") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        with self.lock:
            known = set(self.threads)
        for tid in loaded:
            thread = self.rpc.request("thread/read", {"threadId": tid, "includeTurns": False}).get("thread")
            if not isinstance(thread, dict):
                continue
            if tid not in known:
                # Some releases label the TUI as vscode. Use its originator
                # as additional evidence; do not attach desktop or agent sessions.
                if thread.get("source") != "cli" and not (
                        thread.get("source") == "vscode" and thread.get("originator") == "codex-tui"):
                    continue
                result = self.rpc.request("thread/resume", {"threadId": tid, "excludeTurns": True})
                thread = result.get("thread") or thread
                thread['model']=result.get('model') or thread.get('model','')
            self.ingest({"method": "control/thread", "params": {"thread": thread}})
        for tid in known - loaded:
            self.ingest({"method": "thread/closed", "params": {"threadId": tid}})

    def new_session(self, cwd: str, prompt: str = "", executable: str = "", ephemeral=False):
        if not Path(cwd).is_dir():
            raise CodexError("bad_cwd", "工作目录不存在")
        if not self.connected:
            self.connect(shared=False, executable=executable)
        # New sessions use the user's configured model and require user review for escalation.
        result = self.rpc.request("thread/start", {"cwd": str(Path(cwd).resolve()),
                                  "approvalPolicy": "on-request", "sandbox": "workspace-write",
                                  "approvalsReviewer": "user", "ephemeral": ephemeral})
        thread = result["thread"]
        thread['model']=result.get('model') or thread.get('model','')
        self.ingest({"method": "control/thread", "params": {"thread": thread}})
        if prompt.strip():
            self.send(session_id(thread["id"]), prompt)
        return session_id(thread["id"])

    def ingest(self, message: dict):
        method, params = message.get("method"), message.get("params") or {}
        tid = params.get("threadId")
        now = time.time()
        with self.lock:
            if method in ("control/thread", "thread/started"):
                thread = params.get("thread") or {}
                tid = thread.get("id")
                if not tid or (method == "thread/started" and tid not in self.threads):
                    return
                old = self.threads.get(tid, {})
                self.threads[tid] = {**old, **thread, "alive": True,
                    "last": old.get("last", ""), "items": old.get("items", []),
                    "updated": now, "turn_id": old.get("turn_id", "")}
                return
            if method == "control/disconnected":
                self.error = "Codex 连接已断开"
                self.pending.clear()
                for thread in self.threads.values():
                    thread["alive"] = False
                return
            if tid not in self.threads:
                return
            thread = self.threads[tid]
            thread["updated"] = now
            if method == "thread/status/changed":
                thread["status"] = params.get("status") or {}
            elif method in ("thread/closed", "thread/archived"):
                thread["alive"] = False
                self._clear_requests(tid)
            elif method == "thread/name/updated":
                thread["name"] = params.get("threadName") or params.get("name")
            elif method == "turn/started":
                started=(params.get('turn') or {}).get('id','')
                thread['started_turn_id']=started
                # A fast loopback/model can complete before the turn/start
                # response wakes its caller. Never resurrect that old turn.
                if started and thread.get('completed_turn_id')==started:
                    return
                thread["turn_id"] = started
                thread["status"] = {"type": "active", "activeFlags": []}
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                thread["turn_final"] = turn.get("status", "")
                thread["completed_turn_id"] = turn.get("id") or thread.get("turn_id", "")
                thread["turn_error"] = turn.get("error")
                thread["turn_id"] = ""
                thread["status"] = {"type": "idle"}
                self._clear_requests(tid)
                if turn.get("error"):
                    thread["last"] = clean_text(str(turn["error"].get("message") or "Codex 执行失败"))
                    self._item(thread, "i", thread["last"])
            elif method == "item/agentMessage/delta":
                item_id = params.get("itemId", "")
                if thread.get("delta_id") != item_id:
                    thread["delta_id"], thread["delta"] = item_id, ""
                thread["delta"] = (thread.get("delta", "") + str(params.get("delta") or ""))[-4000:]
                thread["last"] = clean_text(thread["delta"])
            elif method == "item/completed":
                item = params.get("item") or {}
                kind = item.get("type")
                if kind == "agentMessage":
                    thread["last"] = clean_text(item.get("text") or "")
                    self._item(thread, "a", item.get("text") or "")
                elif kind == "commandExecution":
                    self._item(thread, "t", item.get("command") or "")
                    self._item(thread, "o", item.get("aggregatedOutput") or item.get("status") or "")
                elif kind == "fileChange":
                    self._item(thread, "t", "修改：" + ", ".join(c.get("path", "") for c in item.get("changes", [])))
            elif method == "thread/tokenUsage/updated":
                total = ((params.get("tokenUsage") or {}).get("total") or {}).get("outputTokens", 0)
                previous = thread.get("output_tokens")
                thread["output_tokens"] = max(total, previous or 0)
                if previous is not None:
                    thread["new_tokens"] = thread.get("new_tokens", 0) + max(0, total - previous)
            elif method == "serverRequest/resolved":
                for pid, request in list(self.pending.items()):
                    if request["tid"] == tid and request["rid"] == params.get("requestId"):
                        self.pending.pop(pid)
            elif "id" in message and method in (*APPROVALS, INPUT):
                rid = message["id"]
                previous=next((r for r in self.pending.values() if r['tid']==tid and r['rid']==rid),None)
                if previous and previous['method']==method and previous['params']==params:
                    return
                if previous:
                    self.pending.pop(previous['id'],None)
                # Daemon request numbers may restart at zero. A fresh prompt
                # must never reuse a watch approval ID from an earlier turn.
                digest = secrets.token_hex(12)
                pid = f"cxp:{session_key(session_id(tid))}:{digest}"
                old = self.pending.get(pid)
                self.pending[pid] = old or {"id": pid, "tid": tid, "rid": rid, "method": method,
                                           "params": params, "at": now, "step": 0, "answers": {}}

    def _clear_requests(self, tid):
        for pid in [k for k, v in self.pending.items() if v["tid"] == tid]:
            self.pending.pop(pid)

    @staticmethod
    def _item(thread, role, text):
        if not text:
            return
        thread["seq"] = thread.get("seq", 0) + 1
        # Preview formatting must never replace code, paths or full replies.
        # Retain the source alongside the bounded, compatible compact preview.
        raw = str(text)
        thread.setdefault("items", []).append({"s": thread["seq"], "r": role,
            "x": clip(clean_text(raw, 4000), 240), "raw": raw})
        del thread["items"][:-200]

    def snapshot(self):
        with self.lock:
            rows, states = [], {}
            for tid, thread in self.threads.items():
                if not thread.get("alive"):
                    continue
                sid = session_id(tid)
                status = thread.get("status") or {}
                active = status.get("type") == "active"
                waiting = bool(set(status.get("activeFlags") or []) & {"waitingOnApproval", "waitingOnUserInput"})
                name = "Codex · " + str(thread.get("name") or Path(thread.get("cwd") or ".").name or tid[-8:])
                rows.append(IndexRow(0, sid, thread.get("cwd", ""), name,
                                     "busy" if active else "idle", thread.get("updated", 0),
                                     thread.get("createdAt", 0), kind="codex"))
                request = next((r for r in self.pending.values() if r["tid"] == tid), None)
                prompt = None
                if request:
                    params = request["params"]
                    method = request["method"]
                    tool = APPROVALS.get(method, "Codex 提问")
                    hint = params.get("command") or params.get("reason") or params.get("grantRoot") or ""
                    if method == "item/permissions/requestApproval":
                        hint = str(hint) + " " + json.dumps(params.get("permissions") or {}, ensure_ascii=False)
                    if method == INPUT:
                        qs = params.get("questions") or []
                        hint = qs[request["step"]].get("question", "") if qs else "请在电脑回答"
                    prompt = Prompt(request["id"], sid, tool, clip(clean_text(str(hint), 4000), 319), request["at"], kind="codex")
                states[sid] = {"running": active, "waiting": waiting, "prompt": prompt,
                               "last": thread.get("last", ""), "tokens": thread.pop("new_tokens", 0)}
            return rows, states

    def send(self, sid, text, model="", effort="", mode=""):
        if not text.strip():
            raise CodexError("no_lines")
        if not self._actions.acquire(blocking=False):
            raise CodexError("busy")
        try:
            tid = sid.removeprefix("codex:")
            with self.lock:
                thread = self.threads.get(tid)
                if not self.connected or not thread or not thread.get("alive"):
                    raise CodexError("gone")
                if any(r["tid"] == tid for r in self.pending.values()):
                    raise CodexError("asking")
                if (thread.get("status") or {}).get("type") == "active":
                    raise CodexError("busy", "Codex 正在运行；请等完成或先中断")
            params = {"threadId": tid, "input": [{"type": "text", "text": text, "text_elements": []}]}
            if model: params["model"] = model
            if effort: params["effort"] = effort
            if mode:
                mode_model=model or thread.get('model')
                if not mode_model:
                    raise CodexError('model_unknown','先连接并读取 Codex 模型，再选择协作模式')
                params['collaborationMode']={'mode':mode,'settings':{'model':mode_model,
                    'reasoning_effort':effort or None,'developer_instructions':None}}
            result = self.rpc.request("turn/start", params)
            self.ingest({"method": "turn/started", "params": {"threadId": tid, "turn": result.get("turn") or {}}})
            with self.lock:
                self._item(thread, "u", text)
        finally:
            self._actions.release()

    def interrupt(self, sid):
        tid = sid.removeprefix("codex:")
        with self.lock:
            thread = self.threads.get(tid) or {}
            if not self.connected or not thread.get("alive"):
                raise CodexError("gone")
            turn_id = thread.get("turn_id")
            active = (thread.get("status") or {}).get("type") == "active"
        if not active:
            return
        if not turn_id:
            turns = self.rpc.request("thread/read", {"threadId": tid, "includeTurns": True}).get("thread", {}).get("turns", [])
            turn_id = next((t["id"] for t in reversed(turns) if t.get("status") == "inProgress"), None)
        if not turn_id:
            raise CodexError("no_turn")
        self.rpc.request("turn/interrupt", {"threadId": tid, "turnId": turn_id})

    def answer(self, prompt_id, approve):
        with self.lock:
            request = self.pending.get(prompt_id)
            if not request:
                raise CodexError("no_prompt")
            method, params = request["method"], request["params"]
            if method == INPUT:
                raise CodexError("asking", "请从选项页或电脑回答 Codex 的问题")
            if method == "item/permissions/requestApproval":
                result = {"permissions": copy.deepcopy(params.get("permissions") or {}) if approve else {}, "scope": "turn"}
            else:
                available = params.get("availableDecisions")
                decision = "accept" if approve else "decline"
                # Current Windows escalation prompts offer cancel instead of
                # decline. Both deny access; cancel also interrupts the turn.
                if not approve and isinstance(available, list) and "decline" not in available and "cancel" in available:
                    decision = "cancel"
                if isinstance(available, list) and decision not in available:
                    raise CodexError("unsupported_decision")
                result = {"decision": decision}
            self.rpc.respond(request["rid"], result)
            self.pending.pop(prompt_id, None)

    def ask(self, sid, index):
        with self.lock:
            request = next((r for r in self.pending.values() if session_id(r["tid"]) == sid), None)
            if not request:
                return None
            key = session_key(sid)
            digest = request["id"].rsplit(":", 1)[-1]
            step = request["step"]
            params, method = request["params"], request["method"]
            payload = {"id": f"q:{key}:{digest}:{step}", "k": key, "i": index, "kind": "perm", "cur": 0, "esc": 1}
            if method in APPROVALS:
                payload.update(t=APPROVALS[method], q="是否允许这次操作？", o=[{"l": "允许一次"}, {"l": "拒绝"}])
                available = params.get("availableDecisions")
                # The current protocol also offers structured policy amendments.
                # Only one-time accept/decline belong on the watch; dictionaries
                # are intentionally excluded instead of becoming persistent grants.
                if isinstance(available, list):
                    simple = {decision for decision in available if isinstance(decision, str)}
                    if "accept" not in simple or not simple.intersection({"decline", "cancel"}):
                        return None
                preview = params.get("command") or params.get("reason") or params.get("grantRoot") or ""
                if method == "item/permissions/requestApproval":
                    preview = str(preview) + "\n" + json.dumps(params.get("permissions") or {}, ensure_ascii=False)
                payload["ctx"] = clip(str(preview), 1024)
            else:
                questions = params.get("questions") or []
                if step >= len(questions):
                    return None
                question = questions[step]
                options = question.get("options") or []
                # Secret or oversized forms stay on the computer, never truncate selectable answers.
                if question.get("isSecret") or len(options) > 6 or len(options) == 6 and question.get("isOther"):
                    return None
                payload.update(kind="ask", t=clip(str(question.get("header") or "Codex 提问"), 64),
                               q=clip(str(question.get("question") or ""), 240),
                               o=[{"l": clip(str(o.get("label", "")), 96), "d": clip(str(o.get("description", "")), 128)} for o in options])
                if question.get("isOther") or not options:
                    payload["o"].append({"l": "语音回答", "in": 1})
                payload["qn"], payload["qt"] = step + 1, len(questions)
            return Ask(payload["id"], sid, payload, list(range(1, len(payload["o"]) + 1)),
                       inputs={i for i, o in enumerate(payload["o"]) if o.get("in")}, esc=True)

    def pick(self, sid, ask_id, n=0, nav="", text=""):
        with self.lock:
            current = self.ask(sid, -1)
            if current is None or current.id != ask_id:
                raise CodexError("changed")
            request = next(r for r in self.pending.values() if session_id(r["tid"]) == sid)
            if nav == "esc":
                if request["method"] in APPROVALS:
                    self.answer(request["id"], False)
                else:
                    self.interrupt(sid)
                return
            if nav or n < 1 or n > len(current.nums):
                raise CodexError("bad_pick")
            if request["method"] in APPROVALS:
                self.answer(request["id"], n == 1)
                return
            q = request["params"]["questions"][request["step"]]
            if n - 1 in current.inputs:
                if not text.strip():
                    raise CodexError("no_voice")
                answer = text
            else:
                answer = q["options"][n - 1]["label"]
            request["answers"][q["id"]] = {"answers": [answer]}
            if request["step"] + 1 < len(request["params"]["questions"]):
                request["step"] += 1
            else:
                self.rpc.respond(request["rid"], {"answers": copy.deepcopy(request["answers"])})
                self.pending.pop(request["id"], None)

    def context(self, sid, before=-1, after=-1, compact=False):
        with self.lock:
            items = list(self.threads.get(sid.removeprefix("codex:"), {}).get("items", []))
        filtered = [i for i in items if (before < 0 or i["s"] < before) and (after < 0 or i["s"] > after)]
        page = filtered[:8] if after >= 0 else filtered[-8:]
        visible = [{"s": i["s"], "r": i["r"], "x": i["x"] if compact else i.get("raw", i["x"])} for i in page]
        return {"items": visible, "more": bool(page and any(i["s"] < page[0]["s"] for i in items)),
                "newer": bool(page and any(i["s"] > page[-1]["s"] for i in items))}

    def close(self):
        self._stop.set()
        self.rpc.close()  # proxy disconnects only our client; it does not stop the shared CLI daemon
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        self.ingest({"method": "control/disconnected", "params": {}})
