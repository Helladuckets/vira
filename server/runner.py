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
from .session import (EDIT_TOOLS, OUTPUT_CAP, READ_ONLY_EXCLUDE,
                      _extract_plan_md, _finalize_plan, _mark_idea,
                      _plan_ref, _sdk_env, _tool_preview, _tool_summary)

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

# Sentinel on the steering inbox: the owner ended the reply window rather
# than answering. Distinct from a message so an empty Finish can't be
# mistaken for a blank steer.
_END = object()


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
        self.reply_window = float(self.spec.get("reply_window") or 43200)
        self.awaiting_reply = False      # parked at a turn boundary
        # A turn that ended on its own means the work is complete. Ending
        # the reply window after that is the owner saying "I'm done
        # talking" — NOT an abandoned run — so the epilogue (plan publish,
        # idea close-out) must still fire. Reset the moment a reply starts
        # a new turn, so a genuine mid-turn Stop still reads as aborted.
        self.finished_cleanly = False
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
            if self.awaiting_reply:
                # The turn is already over — Stop here is the Finish button,
                # "I have nothing to add", not an abandoned run.
                self.append("[vira] session finished by the owner\n")
                self.inbox.put_nowait(_END)
                return
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
            if self.awaiting_reply:
                self.append("[vira] session closed by the owner\n")
                self.inbox.put_nowait(_END)
                return
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

    # ----- the reply window -----

    async def await_reply(self):
        """Park at a completed turn boundary with the session still open.

        Before this existed the runner ended the session the instant a turn
        finished with an empty inbox, so an agent that closed by asking the
        owner a question was already gone by the time the question was
        read — the compose bar vanishes with the session, and the answer
        had nowhere to go. Now the status stays `running` with awaiting
        "reply", which is exactly what keeps the bar live, and the run only
        finalizes when the owner says so (Finish) or the safety window
        expires. Returns the message to deliver, or None to finish.
        """
        if self.closing or self.interrupted:
            return None
        self.finished_cleanly = True
        self.awaiting_reply = True
        self.state["awaiting"] = "reply"
        self.append("[vira] turn complete — reply to keep going, or Finish "
                    "to close the session\n")
        self.flush_state()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self.inbox.get(),
                                                  self.reply_window)
                except asyncio.TimeoutError:
                    hrs = self.reply_window / 3600
                    self.append(f"[vira] no reply in {hrs:.0f}h — closing "
                                f"the session\n")
                    return None
                if item is _END:
                    return None
                text = (item or "").strip()
                if text:
                    return text
        finally:
            self.awaiting_reply = False
            self.state["awaiting"] = None
            self.flush_state()

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
        if self.spec["mode"] == "acceptedits" and tool_name in EDIT_TOOLS:
            # The middle rung: file edits land unasked, but commands and
            # everything else still raise a card. Deliberately below the
            # read-only denial above, which outranks every allow.
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
                    # Turn boundary: deliver queued steering first.
                    steered = False
                    while not self.inbox.empty():
                        try:
                            item = self.inbox.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if item is _END:
                            continue
                        self.finished_cleanly = False
                        self.append("[vira] steering delivered\n")
                        await client.query(item)
                        steered = True
                    if steered:
                        continue
                    # Nothing queued. The agent has stopped talking — and it
                    # may have just ASKED the owner something (the merge /
                    # test / discard decision every branch session ends on).
                    # Hold the session open so that answer lands in this
                    # conversation instead of arriving after it died.
                    #
                    # Two runs finalize immediately instead. A FAILED turn
                    # must surface as an error now — parking it would show a
                    # dead session as alive for hours and hide exactly the
                    # auth failures the AI-health watcher exists to catch.
                    # And a PLAN session's whole deliverable is published in
                    # the epilogue, so lingering would withhold its own
                    # output; refine a plan by running Plan again.
                    reply = (await self.await_reply()
                             if ok and not spec.get("publish_plan") else None)
                    if reply is None:
                        done = True
                    else:
                        self.finished_cleanly = False
                        self.append("[vira] reply delivered\n")
                        await client.query(reply)
        except Exception as e:  # noqa: BLE001 — session surface, report all
            self.append(f"\n[vira] session failed: {e}\n")
            self.state["error"] = str(e)[:500]
            ok = False
        finally:
            self.client = None
            self.deny_pending("session ended")

        self.state["result_text"] = (result_text or "")[:RESULT_KEEP]
        # Abandoned, not merely ended: a Stop/Close that landed on a
        # COMPLETED turn (the reply window) is the owner closing the door
        # on finished work, so the plan still publishes and the idea still
        # closes out. Only a stop that cut a turn short counts as aborted.
        aborted = (self.interrupted or self.closing) and not self.finished_cleanly
        if aborted:
            self.append("[vira] session interrupted\n")
        # Plan sessions produce markdown read-only; the runner finalizes it
        # (deterministic, survives the server being down): saves it to the
        # vault as a reopenable note, and — on the owner's own machine — also
        # publishes the hosted lab page. Stay "running" until this finishes so
        # the UI streams through to the saved/published references.
        plan_res = None
        if ok and spec.get("publish_plan") and not aborted:
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
        self.awaiting_reply = False
        self.state["status"] = status
        self.state["awaiting"] = None
        self.state["pending"] = []
        self.state["finished"] = time.time()
        if spec.get("idea_id"):
            _mark_idea({"id": spec["id"], "idea_id": spec["idea_id"],
                        "publish_plan": spec.get("publish_plan"),
                        "plan": plan_res,
                        "output": self.output_tail},
                       ok and not aborted, interrupted=aborted)
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
