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

from . import agentbackend, jobfiles, joblog, settings, viratools, worktree
from .session import (EDIT_TOOLS, OUTPUT_CAP, READ_ONLY_EXCLUDE,
                      _extract_plan_md, _finalize_plan, _mark_idea,
                      _plan_ref, _sdk_env, _tool_preview, _tool_summary,
                      norm_mode)

# The SDK is required only by the gated (Anthropic) path. A best-effort
# CLI-exec session must still run on a machine without it, so a failed
# import is recorded rather than fatal; the SDK path reports it loudly the
# moment a job actually needs it.
SDK_IMPORT_ERROR = None
try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )
except Exception as e:  # noqa: BLE001 — tolerated for CLI-exec jobs
    SDK_IMPORT_ERROR = e
    AssistantMessage = ClaudeAgentOptions = ClaudeSDKClient = HookMatcher = None
    PermissionResultAllow = PermissionResultDeny = ResultMessage = None
    SystemMessage = TextBlock = ThinkingBlock = ToolUseBlock = None

# Who the mid-turn steering is from, in the words the model will read.
OWNER_LABEL = settings.get("owner_name") or "The owner"

HEARTBEAT = 2.0
CONTROL_POLL = 0.25
RESULT_KEEP = 20_000

# Sentinel on the steering inbox: the owner ended the reply window rather
# than answering. Distinct from a message so an empty Finish can't be
# mistaken for a blank steer.
_END = object()


class _EngineDone(Exception):
    """Control-flow only: the CLI-exec engine ran to completion inside
    run_session's try block, and the shared epilogue should proceed."""


class Runner:
    END = _END                       # exposed for agentbackend's inbox loop

    def _disarmed_guard(self):
        """The reason this session's branch-first guard cannot fire, or "".

        FAIL CLOSED. Placement and enforcement used to be independent: the
        session was moved into a worktree and the gate was armed by separate
        fields, so when the fields were dropped in transit (2026-07-25 ->
        2026-07-29) the move still happened, everything looked right, and
        the backstop was simply never there. Nothing anywhere said so.

        Binding them means a placed session that arrives without its guard
        is a hard error, not a silent fall-through. The test is the state on
        disk, not a flag someone remembered to set: if the cwd we were handed
        is a linked git worktree, placement happened, so live_root must be
        here too.
        """
        if self.spec.get("read_only"):
            return ""                       # read-only denial covers these
        cwd = self.spec.get("cwd") or ""
        if not cwd or not worktree.is_worktree(cwd):
            return ""                       # not a placed session
        if self.spec.get("worktree") and self.spec.get("live_root"):
            return ""                       # placed AND armed — the good case
        missing = [k for k in ("worktree", "live_root") if not self.spec.get(k)]
        return (f"cwd {cwd} is a linked worktree, so this session was placed "
                f"by branch-first — but the spec is missing {', '.join(missing)}, "
                f"so the guard that keeps writes out of the live checkout "
                f"cannot fire. Refusing to run rather than writing unguarded.")

    def __init__(self, jdir):
        self.dir = Path(jdir)
        self.spec = json.loads((self.dir / "job.json").read_text())
        self.disarmed = self._disarmed_guard()
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
        self.exec_proc = None            # the CLI-exec child, when that path runs
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

    async def steer_hook(self, _input, _tool_use_id, _ctx):
        """Deliver queued steering MID-TURN, alongside the next tool result.

        Before this, `say` only reached the model at the turn boundary in
        run_session — and a turn is however many tool calls the agent
        decides to make. The owner typed "Okay, wrap it up" and watched the
        session run twenty more calls without acknowledging it (2026-07-29);
        from the outside that is indistinguishable from steering being
        broken, and it is why "queued — delivers at the next turn boundary"
        was never a satisfying answer.

        A PostToolUse hook's `additionalContext` is the SDK's channel for
        exactly this: text handed to the model with the tool result it is
        already about to read. So the wait shrinks from "the rest of the
        turn" to "the current tool call".

        Deliberately non-blocking and total: it drains everything queued, it
        never touches the _END sentinel (that is Finish, and only
        await_reply may act on it), and any failure is swallowed — a hook
        that raises must never be able to kill a running session.
        """
        try:
            msgs, hold = [], []
            while True:
                try:
                    item = self.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                (hold if item is _END else msgs).append(item)
            for item in hold:            # put Finish back untouched
                self.inbox.put_nowait(item)
            if not msgs:
                return {}
            for m in msgs:
                self.append(f"[vira] steering delivered — {m}\n")
            body = "\n".join(str(m) for m in msgs)
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"{OWNER_LABEL} sent this mid-turn, just now. Treat it as "
                    f"a live instruction that outranks your current plan — "
                    f"act on it before continuing:\n\n{body}"),
            }}
        except Exception:  # noqa: BLE001 — a hook must never kill the session
            return {}

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
                # Say what is actually about to happen. A parked session is
                # sitting on the inbox (immediate); mid-turn the steer_hook
                # hands it over at the next tool result. Neither is "the
                # next turn boundary", which is what this used to claim —
                # printing it immediately before "reply delivered" told the
                # owner two contradictory things in consecutive lines, and
                # claiming a turn-long wait when steering had genuinely
                # stopped being turn-bound would be worse (2026-07-29).
                if not self.awaiting_reply:
                    self.append("[vira] queued — delivers at the agent's "
                                "next step\n")
        elif op == "permission":
            fut = self.futures.get(cmd.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result((bool(cmd.get("allow")),
                                cmd.get("scope") or "once",
                                cmd.get("reason")))
        elif op == "answer":
            # the owner picked an option (or typed one) on a decision card
            fut = self.futures.get(cmd.get("req_id"))
            if fut is not None and not fut.done():
                fut.set_result(str(cmd.get("answer") or "").strip())
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
        elif self.exec_proc is not None:
            # The CLI-exec path has no in-band interrupt; ending the child
            # ends the turn (the runner's loop then finalizes as aborted).
            try:
                self.exec_proc.terminate()
            except ProcessLookupError:
                pass

    def deny_pending(self, why):
        # Permission futures resolve to (allow, scope, reason); an ask future
        # resolves to the answer STRING. Resolving one with the other's shape
        # would hand the model a tuple as its answer, so the pending entry
        # carries the kind and this reads it.
        kinds = {p.get("req_id"): p.get("kind") for p in self.state["pending"]}
        for req_id, fut in list(self.futures.items()):
            if fut.done():
                continue
            if kinds.get(req_id) == "ask":
                fut.set_result(f"The owner ended the session ({why}). "
                               "Stop and report the open question.")
            else:
                fut.set_result((False, "once", why))

    async def heartbeat_loop(self):
        while True:
            self.flush_state()
            await asyncio.sleep(HEARTBEAT)

    # ----- the reply window -----

    def should_park(self, ok):
        """Whether THIS finished turn holds the session open.

        A method rather than an inline condition because the inline version
        was wrong and untestable: "Stop parks now" shipped on 2026-07-29
        with the fix inside await_reply and the bug in the line deciding
        whether to call it, so nothing the owner could see changed. The
        tests exercised the function and never the guard — both halves, not
        the join, exactly as with the branch guard.

        `ok or self.interrupted`: an interrupted turn is NOT a failed one,
        but the SDK reports them identically — a Stop makes the
        ResultMessage an error, so `ok` is False. A genuine failure still
        must NOT park (a dead session showing as alive for hours would hide
        the auth failures the AI-health watcher exists to catch), which is
        why this reads the interrupt explicitly instead of relaxing `ok`.
        """
        if not self.parks_at_turn_end():
            return False
        return bool(ok) or self.interrupted

    def parks_at_turn_end(self):
        """Whether a completed turn holds the session open for a reply.

        Only a session the OWNER dispatched parks — the window exists so
        the question a coding agent signs off with can be answered. A
        MACHINE-dispatched session has nobody in the terminal and its
        deliverable lands somewhere else entirely (a plan publishes in the
        epilogue, a muse's proposals sit in the Queue's approval bar, a
        judge's verdict is read off the finished run, a circuit stage's
        output feeds the next stage), so parking one only manufactures a
        fake-pending row in Live — and a parked circuit stage would stall
        its whole circuit at the barrier for the length of the window.
        Owner's ruling 2026-07-27: processes like Muse and Plan end
        cleanly with the delivered thing; the approval surfaces already
        exist.
        """
        if self.spec.get("publish_plan"):
            return False
        meta = self.spec.get("meta") or {}
        return not (meta.get("routine_id") or meta.get("circuit_run")
                    or meta.get("judge_of"))

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
        # ONLY Finish/close ends the session here. A Stop does NOT — it ends
        # the current turn, which is a pause, not an abandonment. Stopping
        # something mid-work is precisely the moment the owner has something
        # to say to it, and until 2026-07-29 it was the one moment Vira took
        # the input box away: the agent received the queued steer, acted on
        # it, wrapped up cleanly, and the session closed anyway because
        # `interrupted` was set.
        if self.closing:
            return None
        # A cut-short turn is still NOT "finished cleanly" — that flag is
        # what decides whether the epilogue treats the run as complete
        # (publishing a plan, closing out the idea). Parking after a Stop
        # must not smuggle an interrupted run into that path; the owner can
        # reply to finish the work, and the reply's own clean turn is what
        # earns the flag.
        if not self.interrupted:
            self.finished_cleanly = True
        self.awaiting_reply = True
        # "reply" = the turn ended on its own, so the work is COMPLETE.
        # "paused" = a Stop cut it short. Both hold the box open; they are
        # separated because the surfaces must not call an interrupted run
        # complete — jobPhase reads this to decide what the LED and the
        # strip say (app.js).
        self.state["awaiting"] = "paused" if self.interrupted else "reply"
        # NOTHING IS APPENDED HERE (owner's call, 2026-07-29). This used to
        # print "turn complete — reply to keep going, or Finish to close the
        # session", which restated what the compose bar was already saying
        # in its own placeholder and its own button labels: the bar is live,
        # it reads "Reply — this session is holding open for you", and the
        # button says Finish. A line of chrome dressed as Vira speaking, at
        # the exact spot the owner reads for the session's CONCLUSION.
        # The close-out is the agent's job and is specified in the preamble
        # (viratools.preamble, "HOW TO END A TURN"); the harness must not
        # talk over it.
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

    # ----- the owner channel: questions, not permissions -----

    async def ask_owner(self, question, options, allow_text=True):
        """Raise a DECISION card and block until the owner picks.

        This exists because of a real asymmetry the owner hit on 2026-07-25:
        a tool call the session could not make on its own produced a rich
        clickable Approve/Deny card, while a QUESTION the session needed
        answered produced only a line of prose above a free-text box. The
        first is impossible to miss; the second is a paragraph in a
        transcript on a phone. So the sessions that stopped to ask were the
        ones that quietly never finished.

        Same rails as the gate — a pending entry plus a future — so the card
        is delivered by the machinery that already proved it reaches the
        owner. Only `kind` differs, which is what the UI switches on.
        """
        q = (question or "").strip()
        if not q:
            return "No question was asked."
        # Options arrive as [{label, description}] (viratools.parse_options
        # normalizes both shapes). Keep the description — it is what makes
        # the choice answerable on a phone, and dropping it here would quietly
        # undo the whole point of the card.
        opts = []
        for o in (options or []):
            if isinstance(o, dict):
                label = str(o.get("label") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                label, desc = str(o).strip(), ""
            if label:
                opts.append({"label": label, "description": desc})
        # Cap AFTER dropping blanks — a blank must not eat one of the six
        # slots the owner actually gets to choose from.
        opts = opts[:6]
        req_id = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.futures[req_id] = fut
        self.state["pending"].append({
            "req_id": req_id, "kind": "ask", "question": q,
            "options": opts, "allow_text": bool(allow_text),
            "summary": q, "created": time.time(),
        })
        self.state["awaiting"] = "ask"
        self.append(f"[vira] question for you — {q}\n")
        for i, o in enumerate(opts, 1):
            self.append(f"    {i} — {o['label']}\n")
            if o["description"]:
                self.append(f"        {o['description']}\n")
        self.flush_state()
        timeout = float(self.spec.get("ask_timeout") or 21600)
        try:
            answer = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            answer = None
        finally:
            self.futures.pop(req_id, None)
            self.state["pending"] = [p for p in self.state["pending"]
                                     if p["req_id"] != req_id]
            self.state["awaiting"] = ("permission" if self.state["pending"]
                                      else None)
            self.flush_state()
        if answer is None:
            self.append("[vira] no answer within the window — the session "
                        "should stop and report the question\n")
            # Deliberately NOT a default choice. Guessing is what produced
            # a half-applied feature in the first place; an unanswered
            # question must end the work, not silently pick a branch.
            return ("The owner did not answer in time. Do NOT guess or pick "
                    "an option yourself. Stop the work here, leave what you "
                    "have in a consistent state, and put this question at "
                    "the top of your final report.")
        self.append(f"[you] {answer}\n")
        return answer

    # ----- the permission gate -----

    async def gate(self, tool_name, tool_input, context):  # noqa: ARG002
        if self.spec.get("read_only"):
            # Read-only policy FIRST (audit P1-4): the denial outranks every
            # allow list — session grants never apply, and READ_ONLY_EXCLUDE
            # strips Task/WebSearch/the one native write from the read set.
            #
            # Keyed on read_only ALONE since 2026-08-04. publish_plan used to
            # imply it, which made "produce a plan" and "touch nothing" the
            # same switch — so a planning stage could not search the web or
            # spawn a subagent (READ_ONLY_EXCLUDE strips both) purely because
            # of what it was going to do with its output. A plan is a shape,
            # not a rung: a stage that wants both still sets read_only.
            if (tool_name in self.auto_allow
                    and tool_name not in READ_ONLY_EXCLUDE):
                return PermissionResultAllow()
            summary = _tool_summary({"name": tool_name, "input": tool_input})
            self.append(f"[vira] denied (read-only session) — {summary}\n")
            return PermissionResultDeny(
                message="This session is read-only. Do not modify anything "
                        "or retry this call — work from what the "
                        "auto-allowed read tools can see and describe any "
                        "needed change in your final report.")
        # Branch-first backstop. Placement alone does not hold: an agent can
        # still write an absolute path back into the live checkout, which is
        # exactly what happened on 2026-07-25. This denial outranks the
        # auto-allow set and session grants — like the read-only rule above,
        # because a rule that any allow-list can override is not a rule.
        wt, live_root = self.spec.get("worktree"), self.spec.get("live_root")
        if wt and live_root and tool_name in worktree.WRITE_TOOLS:
            targets = (worktree.bash_targets(tool_input) if tool_name == "Bash"
                       else worktree.target_paths(tool_input))
            for p in targets:
                if worktree.violates(p, live_root, wt):
                    self.append(f"[vira] denied (branch-first) — {tool_name} "
                                f"targets the live checkout: {p}\n")
                    return PermissionResultDeny(
                        message=worktree.deny_message(wt))
        if tool_name in self.auto_allow or tool_name in self.session_allow:
            return PermissionResultAllow()
        mode = norm_mode(self.spec.get("mode"), "manual")
        if mode == "bypassPermissions":
            # The top rung allows everything — but it is the GATE saying so,
            # not the gate being absent. Until 2026-07-29 this rung passed
            # can_use_tool=None, which removed the two denials above along
            # with the cards; the branch-first backstop the rung most needs
            # was the thing it switched off. Claude Code's own
            # bypassPermissions keeps a circuit breaker for the same reason;
            # the live-tree denial is ours.
            return PermissionResultAllow()
        if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
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
            # The RESOLVED generation, which is the only place it is ever
            # stated. A launch asks for a TIER ("opus") and the CLI picks the
            # id — and it does not always pick the newest: on CLI 2.1.207
            # `opus` resolves to claude-opus-4-8 while claude-opus-5 answers
            # fine. app.js has read `model_used` in three places since it was
            # written and nothing ever wrote it, so every surface fell back
            # to the requested alias and showed "Opus" over a 4.8 session.
            # Recording it is what makes a stale alias visible instead of
            # silent — the ledger keeps it after the job dir is pruned.
            if model and self.state.get("model_used") != model:
                self.state["model_used"] = model
                joblog.record_model_used(self.spec["id"], model)
            if sid:
                self.flush_state()
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
            self.append(spec.get("branch_note") or "")
            if self.disarmed:
                # Fail closed — see _disarmed_guard. This must raise BEFORE
                # any engine starts; a session that gets as far as its first
                # tool call has already been able to write.
                self.append(f"[vira] REFUSING TO RUN (branch-first guard "
                            f"disarmed) — {self.disarmed}\n")
                raise RuntimeError(f"branch-first guard disarmed: "
                                   f"{self.disarmed}")
            if agentbackend.uses_cli_exec(spec):
                # Best-effort engine: the provider's own CLI, inside this
                # same harness — inbox, reply window, epilogue all shared.
                result_text, ok = await agentbackend.run_cliexec(self)
                raise _EngineDone
            if SDK_IMPORT_ERROR is not None:
                raise RuntimeError(
                    f"claude-agent-sdk unavailable: {SDK_IMPORT_ERROR}")
            # The owner channel: a tool the session can call when it needs a
            # DECISION, not a permission. Bound per-process because a runner
            # supervises exactly one session, so there is no ambiguity about
            # whose transcript the question belongs in.
            viratools.bind_ask(self.ask_owner)
            vira_srv = viratools.sdk_server()
            options = ClaudeAgentOptions(
                cwd=spec["cwd"],
                model=spec.get("model_resolved") or spec.get("model"),
                env=_sdk_env(),
                # The SDK default is a near-empty system prompt; opt into the
                # full Claude Code harness prompt, with the Vira preamble
                # appended — the deep Vira connection.
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": viratools.preamble(
                                   worktree_path=spec.get("worktree") or "",
                                   branch=spec.get("branch") or "",
                                   live_root=spec.get("live_root") or "")},
                mcp_servers={"vira": vira_srv} if vira_srv else {},
                allowed_tools=list(viratools.TOOL_NAMES) if vira_srv else [],
                # ALWAYS "default" + ALWAYS our gate. Handing the SDK its own
                # bypassPermissions used to skip can_use_tool altogether,
                # which took the read-only and branch-first denials with it —
                # policy lived in the gate, so removing the gate removed the
                # policy. The bypass rung is now expressed INSIDE the gate
                # (it returns Allow), so there is exactly one place that
                # decides what a session may do, in every mode.
                permission_mode="default",
                can_use_tool=self.gate,
                # Steering reaches the model at the next TOOL RESULT, not at
                # the end of the turn — see steer_hook. matcher=None so it
                # fires after every tool, which is the point: the owner
                # should never have to wait out a fifty-call turn to be
                # heard. The turn-boundary drain in run_session stays as the
                # fallback for a turn that calls no tools at all.
                hooks={"PostToolUse": [HookMatcher(matcher=None,
                                                   hooks=[self.steer_hook])]},
                # Read-only sessions: write tools — and the excluded
                # non-reads (Task subagents, WebSearch egress) — leave the
                # model's context entirely; anything else risky is denied
                # by the gate.
                disallowed_tools=(["Write", "Edit", "NotebookEdit",
                                   "Task", "WebSearch"]
                                  if spec.get("read_only") else []),
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
                    # output; refine a plan by running Plan again. The same
                    # goes for every machine-dispatched run — see
                    # parks_at_turn_end for the full reasoning.
                    reply = (await self.await_reply()
                             if self.should_park(ok) else None)
                    if reply is None:
                        done = True
                    else:
                        self.finished_cleanly = False
                        # The reply answers the Stop, so the interrupt is
                        # served: this turn starts clean and is judged on
                        # its own ending. Without the reset, one Stop would
                        # mark every later turn of the session aborted.
                        self.interrupted = False
                        self.append("[vira] reply delivered\n")
                        await client.query(reply)
        except _EngineDone:
            pass                     # CLI-exec engine finished; epilogue below
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
