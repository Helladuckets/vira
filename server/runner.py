"""Detached durable job runner — a live agent session that outlives Vira.

Spawned by the session registry as its own process group
(.venv python -m server.runner data/jobs/<id>), so a Vira restart —
launchctl kickstart, crash, update-and-restart — no longer kills running
jobs. The runner owns the Claude Agent SDK session end to end: it streams
the transcript to output.log, mirrors status / pending permission cards /
heartbeat into state.json, and tails control.jsonl for the owner's
steering, permission decisions, interrupts, and closes (appended by
whichever server process is up — including one booted AFTER this runner
started; the supervisor re-attaches through these same files).

Everything the in-process session had still applies here: the claude_code
system-prompt preset with the Vira preamble, the in-process mcp "vira"
native tools (imported from viratools — they read Vira's data plane
directly from disk/Keychain, so they keep working even while the server
itself is down), the permission gate with timeout default-deny, plan
publishing, and closing out the launching idea. The runner finalizes its
own joblog record; the stores are cross-process safe (filelock).
"""
import asyncio
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from . import jobfiles, joblog, viratools
from .session import (OUTPUT_CAP, READ_ONLY_EXCLUDE, _extract_plan_md,
                      _finalize_plan, _mark_idea, _plan_ref, _sdk_env,
                      _tool_preview, _tool_summary)

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )
except Exception as e:  # noqa: BLE001 — the server checked, but be loud
    print(f"[runner] claude-agent-sdk unavailable: {e}", flush=True)
    sys.exit(78)  # EX_CONFIG

HEARTBEAT = 2.0
CONTROL_POLL = 0.25
RESULT_KEEP = 20_000


class Runner:
    def __init__(self, jdir):
        self.dir = Path(jdir)
        self.spec = json.loads((self.dir / "job.json").read_text())
        self.state = {
            "id": self.spec["id"], "status": "running",
            "started": self.spec.get("started") or time.time(),
            "finished": None, "session_id": "", "awaiting": None,
            "pending": [], "result_text": "", "heartbeat": time.time(),
            "pid": os.getpid(), "mode": self.spec["mode"], "live": True,
            "error": "",
        }
        self.out = open(self.dir / "output.log", "a", encoding="utf-8")
        self.output_tail = ""            # rolling copy (plan-URL search)
        self.inbox = asyncio.Queue()     # queued steering messages
        self.futures = {}                # req_id -> asyncio.Future
        self.session_allow = set()       # "approve for session" grants
        self.auto_allow = (set(self.spec.get("auto_allow") or [])
                           | set(viratools.TOOL_NAMES))
        self.client = None
        self.closing = False
        self.interrupted = False
        self._consumed = 0               # control.jsonl lines handled
        self.flush_state()

    # ----- files -----

    def flush_state(self):
        self.state["heartbeat"] = time.time()
        jobfiles.write_json_atomic(self.dir / "state.json", self.state)

    def append(self, piece):
        if not piece:
            return
        self.out.write(piece)
        self.out.flush()
        self.output_tail = (self.output_tail + piece)[-OUTPUT_CAP:]

    # ----- control stream -----

    async def control_loop(self):
        while True:
            self._consumed, cmds = jobfiles.read_control(
                self.dir, self._consumed)
            for cmd in cmds:
                try:
                    await self.handle(cmd)
                except Exception as e:  # noqa: BLE001 — never wedge the tail
                    self.append(f"[vira] control error: {e}\n")
            await asyncio.sleep(CONTROL_POLL)

    async def handle(self, cmd):
        op = cmd.get("op")
        if op == "say":
            text = (cmd.get("text") or "").strip()
            if text:
                self.inbox.put_nowait(text)
                self.append(f"[you] {text}\n")
                self.append("[vira] queued — delivers at the next turn "
                            "boundary\n")
        elif op == "permission":
            fut = self.futures.get(cmd.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result((bool(cmd.get("allow")),
                                cmd.get("scope") or "once",
                                cmd.get("reason")))
        elif op == "interrupt":
            self.interrupted = True
            self.deny_pending("interrupted by the owner")
            self.append("[vira] interrupt requested — stopping at the next "
                        "boundary…\n")
            await self.do_interrupt()
        elif op == "close":
            self.closing = True
            while not self.inbox.empty():
                try:
                    self.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self.interrupted = True
            self.deny_pending("session closed by the owner")
            self.append("[vira] session closed by the owner\n")
            await self.do_interrupt()

    async def do_interrupt(self):
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception as e:  # noqa: BLE001 — surface, don't crash
                self.append(f"[vira] interrupt failed: {e}\n")

    def deny_pending(self, why):
        for fut in list(self.futures.values()):
            if not fut.done():
                fut.set_result((False, "once", why))

    async def heartbeat_loop(self):
        while True:
            self.flush_state()
            await asyncio.sleep(HEARTBEAT)

    # ----- the permission gate -----

    async def gate(self, tool_name, tool_input, context):  # noqa: ARG002
        if self.spec.get("publish_plan") or self.spec.get("read_only"):
            # Read-only policy FIRST (audit P1-4): the denial outranks every
            # allow list — session grants never apply, and READ_ONLY_EXCLUDE
            # strips Task/WebSearch/the one native write from the read set.
            if (tool_name in self.auto_allow
                    and tool_name not in READ_ONLY_EXCLUDE):
                return PermissionResultAllow()
            summary = _tool_summary({"name": tool_name, "input": tool_input})
            kind = ("plan" if self.spec.get("publish_plan")
                    else "read-only")
            self.append(f"[vira] denied ({kind} sessions are read-only) — "
                        f"{summary}\n")
            return PermissionResultDeny(
                message="This session is read-only. Do not modify anything "
                        "or retry this call — work from what the "
                        "auto-allowed read tools can see and describe any "
                        "needed change in your final report.")
        if tool_name in self.auto_allow or tool_name in self.session_allow:
            return PermissionResultAllow()
        summary = _tool_summary({"name": tool_name, "input": tool_input})
        req_id = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.futures[req_id] = fut
        self.state["pending"].append({
            "req_id": req_id, "tool": tool_name, "summary": summary,
            "preview": _tool_preview(tool_name, tool_input),
            "created": time.time(),
        })
        self.state["awaiting"] = "permission"
        self.append(f"[vira] permission needed — {summary}\n")
        self.flush_state()
        timeout = float(self.spec.get("permission_timeout") or 600)
        try:
            allow, scope, reason = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            allow, scope, reason = False, "once", None
            self.append(f"[vira] permission timed out after {int(timeout)}s "
                        f"— denied: {summary}\n")
        finally:
            self.futures.pop(req_id, None)
            self.state["pending"] = [p for p in self.state["pending"]
                                     if p["req_id"] != req_id]
            self.state["awaiting"] = ("permission" if self.state["pending"]
                                      else None)
            self.flush_state()
        if allow:
            if scope == "session":
                self.session_allow.add(tool_name)
                self.append(f"[vira] approved for this session — "
                            f"{tool_name}\n")
            else:
                self.append(f"[vira] approved — {summary}\n")
            return PermissionResultAllow()
        note = f" ({reason})" if reason else ""
        self.append(f"[vira] denied{note} — {summary}\n")
        return PermissionResultDeny(
            message=(reason or "Denied by the owner.")
            + " Do not retry this call; adjust your approach or finish "
              "with what you have.")

    # ----- transcript rendering -----

    def render_message(self, msg):
        """Same shapes the in-process path produced, so renderTermLine keeps
        working. Returns (result_text, ok) on the terminal ResultMessage."""
        if isinstance(msg, AssistantMessage):
            out = ""
            for b in msg.content:
                if isinstance(b, TextBlock):
                    txt = (b.text or "").strip()
                    if txt:
                        out += txt + "\n"
                elif isinstance(b, ToolUseBlock):
                    out += "  → " + _tool_summary(
                        {"name": b.name, "input": b.input}) + "\n"
                elif isinstance(b, ThinkingBlock):
                    pass  # keep the log readable, as before
            self.append(out)
            return None
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            sid = msg.data.get("session_id") or ""
            if sid and not self.state["session_id"]:
                self.state["session_id"] = sid
                self.flush_state()
                joblog.record_session(self.spec["id"], sid)
            tail = f" (session {sid[:8]})" if sid else ""
            model = msg.data.get("model", "claude")
            self.append(f"[vira] {model} working…{tail}\n")
            return None
        if isinstance(msg, ResultMessage):
            return (msg.result or "", not msg.is_error)
        return None

    # ----- the session -----

    async def run_session(self):
        spec = self.spec
        result_text = ""
        ok = False
        try:
            vira_srv = viratools.sdk_server()
            options = ClaudeAgentOptions(
                cwd=spec["cwd"],
                model=spec.get("model_resolved") or spec.get("model"),
                env=_sdk_env(),
                # The SDK default is a near-empty system prompt; opt into the
                # full Claude Code harness prompt, with the Vira preamble
                # appended — the deep Vira connection.
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": viratools.preamble()},
                mcp_servers={"vira": vira_srv} if vira_srv else {},
                allowed_tools=list(viratools.TOOL_NAMES) if vira_srv else [],
                permission_mode=("bypassPermissions"
                                 if spec["mode"] == "autopilot"
                                 else "default"),
                can_use_tool=(None if spec["mode"] == "autopilot"
                              else self.gate),
                # Plan and read-only sessions: write tools — and the
                # excluded non-reads (Task subagents, WebSearch egress) —
                # leave the model's context entirely; anything else risky
                # is denied by the gate.
                disallowed_tools=(["Write", "Edit", "NotebookEdit",
                                   "Task", "WebSearch"]
                                  if spec.get("publish_plan")
                                  or spec.get("read_only") else []),
            )
            async with ClaudeSDKClient(options) as client:
                self.client = client
                await client.query(spec["prompt"])
                done = False
                while not done:
                    async for msg in client.receive_response():
                        r = self.render_message(msg)
                        if r is not None:
                            result_text, ok = r
                    if self.closing:
                        break
                    # Turn boundary: deliver queued steering, else finish.
                    steered = False
                    while not self.inbox.empty():
                        try:
                            text = self.inbox.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        self.append("[vira] steering delivered\n")
                        await client.query(text)
                        steered = True
                    if not steered:
                        done = True
        except Exception as e:  # noqa: BLE001 — session surface, report all
            self.append(f"\n[vira] session failed: {e}\n")
            self.state["error"] = str(e)[:500]
            ok = False
        finally:
            self.client = None
            self.deny_pending("session ended")

        self.state["result_text"] = (result_text or "")[:RESULT_KEEP]
        if self.interrupted or self.closing:
            self.append("[vira] session interrupted\n")
        # Plan sessions produce markdown read-only; the runner finalizes it
        # (deterministic, survives the server being down): saves it to the
        # vault as a reopenable note, and — on the owner's own machine — also
        # publishes the hosted lab page. Stay "running" until this finishes so
        # the UI streams through to the saved/published references.
        plan_res = None
        if ok and spec.get("publish_plan") and not (self.interrupted
                                                    or self.closing):
            md = _extract_plan_md(result_text or self.output_tail)
            self.append("\n[vira] saving the plan…\n")
            plan_res = await asyncio.to_thread(
                _finalize_plan, md, spec.get("idea_id"), spec["id"])
            self.append((
                f"[vira] plan saved: {_plan_ref(plan_res)}\n"
                if plan_res.get("plan_id") else
                "[vira] plan could not be saved — see runner.log\n"))
            if plan_res.get("url"):
                self.append(f"[vira] plan published: {plan_res['url']}\n")
        status = ("done" if ok or self.interrupted or self.closing
                  else "error")
        self.state["status"] = status
        self.state["awaiting"] = None
        self.state["pending"] = []
        self.state["finished"] = time.time()
        if spec.get("idea_id"):
            _mark_idea({"id": spec["id"], "idea_id": spec["idea_id"],
                        "publish_plan": spec.get("publish_plan"),
                        "plan": plan_res,
                        "output": self.output_tail},
                       ok and not (self.interrupted or self.closing),
                       interrupted=self.interrupted or self.closing)
        joblog.record_finish(spec["id"], status,
                             result_text or self.state["error"])
        self.flush_state()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # A polite kill (system shutdown, manual TERM) ends the turn and
            # finalizes state instead of leaving a running-forever record.
            loop.add_signal_handler(
                sig, lambda: asyncio.ensure_future(
                    self.handle({"op": "close"})))
        hb = asyncio.ensure_future(self.heartbeat_loop())
        ctl = asyncio.ensure_future(self.control_loop())
        try:
            await self.run_session()
        finally:
            hb.cancel()
            ctl.cancel()
            self.out.close()


def main():
    if len(sys.argv) != 2:
        print("usage: python -m server.runner <job-dir>", flush=True)
        sys.exit(64)
    runner = Runner(sys.argv[1])
    try:
        asyncio.run(runner.run())
    except Exception as e:  # noqa: BLE001 — last-resort finalization
        runner.state["status"] = "error"
        runner.state["error"] = str(e)[:500]
        runner.state["finished"] = time.time()
        try:
            jobfiles.write_json_atomic(runner.dir / "state.json",
                                       runner.state)
            joblog.record_finish(runner.spec["id"], "error", str(e))
        finally:
            raise


if __name__ == "__main__":
    main()
