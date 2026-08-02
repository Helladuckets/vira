"""The Forge's product model over Vira's existing orchestration engines.

A Flow is the stable object the owner edits in the Forge. Existing circuits
remain the execution authority and existing routines remain the scheduling
authority; this module supplies the visual graph, version metadata, Kit
catalog, and the lossless adapters between those two proven stores.

The graph sidecar is deliberately not a second circuit store. Prompts, models,
permissions, dependencies, run state, and cadence continue to live in their
existing canonical stores. The sidecar owns only visual/editor information
that those stores cannot represent: node placement, port-level wire metadata,
attached context references, and revision numbers.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import actions, circuits, jsonstore, routines

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "flow-graphs.json"
LIBRARY = Path.home() / ".claude"

MAX_NODES = 80
MAX_EDGES = 240
MAX_CONTEXTS = 80
MAX_TEXT = 24_000
MAX_SOURCE = 250_000
SOURCE_SUFFIXES = {".md", ".txt", ".py", ".sh", ".zsh", ".js", ".ts",
                   ".json", ".yaml", ".yml", ".toml", ".html", ".css"}
AGENT_NODE_TYPES = {"agent", "judge"}
EXECUTABLE_NODE_TYPES = AGENT_NODE_TYPES | {"logic", "approval", "output", "native"}
EDITABLE_NODE_TYPES = EXECUTABLE_NODE_TYPES
DISPLAY_NODE_TYPES = EDITABLE_NODE_TYPES | {
    "trigger", "context", "tool", "native", "system", "connector"
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank():
    return {"graphs": {}}


def _store():
    value = jsonstore.read(STORE, _blank())
    if not isinstance(value, dict) or not isinstance(value.get("graphs"), dict):
        return _blank()
    return value


def _slug(value, prefix="stage"):
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or prefix)[:48]


def _layout(stages):
    """Deterministic columns from dependency depth, rows by source order."""
    by_id = {stage["id"]: stage for stage in stages}
    depth = {}

    def visit(sid, seen=None):
        if sid in depth:
            return depth[sid]
        seen = set(seen or ())
        if sid in seen:
            return 0
        seen.add(sid)
        needs = [n for n in by_id[sid].get("needs") or [] if n in by_id]
        depth[sid] = 0 if not needs else max(visit(n, seen) for n in needs) + 1
        return depth[sid]

    for sid in by_id:
        visit(sid)
    rows = {}
    out = {}
    for stage in stages:
        col = depth.get(stage["id"], 0)
        row = rows.get(col, 0)
        rows[col] = row + 1
        out[stage["id"]] = {"x": 150 + col * 310, "y": 130 + row * 180}
    return out


def _node(stage, placed):
    mode = circuits.norm_stage_mode(stage.get("mode"))
    node_type = mode if mode in {"judge", "logic", "approval", "output", "native"} else "agent"
    descriptions = {
        "agent": "One durable Vira agent stage.",
        "judge": "Grades an upstream result and controls retry behavior.",
        "logic": "A deterministic gate that routes work without launching a model.",
        "approval": "Pauses the Flow until an explicit human decision is recorded.",
        "output": "Shapes the finished result for its named destination.",
        "native": "Launches an existing Vira system inside this Flow.",
    }
    data = {
        "id": stage["id"],
        "type": node_type,
        "name": stage.get("name") or stage["id"],
        "description": descriptions[node_type],
        "x": int(placed.get("x", 0)),
        "y": int(placed.get("y", 0)),
        "expanded": bool(placed.get("expanded")),
        "width": int(placed.get("width") or 244),
        "height": int(placed.get("height") or 148),
        "model": stage.get("model") or "",
        "mode": mode,
        "read_only": bool(stage.get("read_only")) or node_type == "judge",
        "prompt": stage.get("prompt") or "",
        "extra": stage.get("extra") or "",
        "judge": dict(stage.get("judge") or {}),
        "logic": dict(stage.get("logic") or {}),
        "approval": dict(stage.get("approval") or {}),
        "output": dict(stage.get("output") or {}),
        "forge": dict(stage.get("forge") or {}),
        "source": "circuit-stage",
    }
    return data


def _edge(source, target, meta=None):
    meta = meta or {}
    return {
        "id": meta.get("id") or f"edge:{source}:{target}",
        "from": source,
        "to": target,
        "from_port": meta.get("from_port") or "result",
        "to_port": meta.get("to_port") or "input",
        "label": str(meta.get("label") or "")[:200],
        "instructions": str(meta.get("instructions") or "")[:2000],
        "direction": "both" if meta.get("direction") == "both" else "forward",
    }


def _graph_for_circuit(circuit, rows, saved):
    stages = [stage for stage in circuit.get("stages") or []
              if not (stage.get("forge") or {}).get("system")]
    defaults = _layout(stages)
    saved_nodes = {n.get("id"): n for n in saved.get("nodes") or []
                   if isinstance(n, dict) and n.get("id")}
    nodes = [_node(stage, {**defaults.get(stage["id"], {}),
                           **saved_nodes.get(stage["id"], {})})
             for stage in stages]
    stage_ids = {stage["id"] for stage in stages}
    for stored in saved_nodes.values():
        if (stored.get("id") in stage_ids
                or str(stored.get("id") or "").startswith("routine:")
                or stored.get("type") not in DISPLAY_NODE_TYPES):
            continue
        nodes.append(dict(stored))
    edge_meta = {(e.get("from"), e.get("to")): e
                 for e in saved.get("edges") or [] if isinstance(e, dict)}
    edges = []
    for stage in stages:
        for need in stage.get("needs") or []:
            edges.append(_edge(need, stage["id"], edge_meta.get((need, stage["id"]))))
    for saved_edge in saved.get("edges") or []:
        if not isinstance(saved_edge, dict):
            continue
        pair = (saved_edge.get("from"), saved_edge.get("to"))
        if pair in {(edge["from"], edge["to"]) for edge in edges}:
            continue
        if pair[0] in saved_nodes and pair[1] in saved_nodes:
            edges.append(_edge(pair[0], pair[1], saved_edge))
    triggers = [dict(r) for r in rows if r.get("kind") == "circuit"
                and r.get("circuit_id") == circuit["id"]]
    entry_ids = [stage["id"] for stage in stages if not stage.get("needs")]
    for index, trigger in enumerate(triggers):
        trigger_id = f"routine:{trigger['id']}"
        nodes.append({
            "id": trigger_id, "type": "trigger",
            "name": trigger.get("name") or "Schedule",
            "description": _cadence(trigger),
            "x": 25, "y": 120 + index * 170, "width": 220, "height": 126,
            "source": "routine", "routine_id": trigger["id"],
            "daily_at": trigger.get("daily_at") or "",
            "every_hours": trigger.get("every_hours"),
            "enabled": bool(trigger.get("enabled")),
            "notify": bool(trigger.get("notify")),
        })
        for entry_id in entry_ids:
            edges.append(_edge(trigger_id, entry_id,
                               edge_meta.get((trigger_id, entry_id))))
    return {
        "id": circuit["id"],
        "name": circuit.get("name") or circuit["id"],
        "description": circuit.get("description") or "",
        "kind": "flow",
        "builtin": bool(circuit.get("builtin")),
        "revision": max(1, int(saved.get("revision") or 1)),
        "created": circuit.get("created"),
        "updated": circuit.get("updated"),
        "nodes": nodes,
        "edges": edges,
        "contexts": list(saved.get("contexts") or [])[:MAX_CONTEXTS],
        "triggers": triggers,
        "executable": True,
    }


def _graph_for_routine(row):
    rid = row["id"]
    trigger = {
        "id": f"{rid}:trigger", "type": "trigger", "name": "Schedule",
        "description": _cadence(row), "x": 130, "y": 170,
        "width": 210, "height": 126, "source": "routine",
        "routine_id": rid, "daily_at": row.get("daily_at") or "",
        "every_hours": row.get("every_hours"),
        "enabled": bool(row.get("enabled")), "notify": bool(row.get("notify")),
    }
    native = {
        "id": f"{rid}:native", "type": "native", "name": row.get("name") or rid,
        "description": row.get("description") or "Native Vira operation.",
        "x": 470, "y": 170, "width": 280, "height": 148,
        "prompt": row.get("prompt") or "", "model": row.get("model") or "",
        "mode": row.get("mode") or "", "source": "routine-native",
        "routine_id": rid, "source_ref": rid, "locked": True,
    }
    return {
        "id": f"routine:{rid}", "source_id": rid,
        "name": row.get("name") or rid, "description": row.get("description") or "",
        "kind": "native", "builtin": not rid.startswith("rt_"), "revision": 1,
        "created": row.get("created"), "updated": row.get("last_run"),
        "nodes": [trigger, native],
        "edges": [_edge(trigger["id"], native["id"])],
        "contexts": [], "triggers": [dict(row)], "executable": True,
    }


def _cadence(row):
    if row.get("daily_at"):
        return f"Daily at {row['daily_at']}"
    hours = row.get("every_hours")
    return f"Every {hours:g} hours" if isinstance(hours, (int, float)) else "Manual"


def list_flows():
    sidecar = _store()["graphs"]
    rows = routines.list_routines()
    result = [_graph_for_circuit(c, rows, sidecar.get(c["id"]) or {})
              for c in circuits.list_circuits()]
    attached = {r["id"] for r in rows if r.get("kind") == "circuit"
                and r.get("circuit_id")}
    result.extend(_graph_for_routine(r) for r in rows if r["id"] not in attached)
    return result


def get_flow(flow_id):
    return next((flow for flow in list_flows() if flow["id"] == flow_id), None)


def _clean_contexts(values):
    out = []
    for raw in values or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("title") or "Context").strip()[:200]
        out.append({
            "id": str(raw.get("id") or "ctx_" + uuid.uuid4().hex[:10])[:100],
            "name": name,
            "kind": str(raw.get("kind") or "reference")[:40],
            "ref": str(raw.get("ref") or raw.get("path") or "")[:1000],
            "note": str(raw.get("note") or "")[:2000],
        })
        if len(out) >= MAX_CONTEXTS:
            break
    return out


def _expand_systems(nodes, edges):
    """Compile anchored Flow instances inline while preserving their box."""
    nodes = [dict(node) for node in nodes]
    edges = [dict(edge) for edge in edges]
    for _depth in range(3):
        systems = [node for node in nodes if node.get("type") == "system"]
        if not systems:
            return nodes, edges
        changed = False
        for system in systems:
            embedded = system.get("embedded") or {}
            children = [dict(node) for node in embedded.get("nodes") or []]
            child_edges = [dict(edge) for edge in embedded.get("edges") or []]
            if not children:
                continue
            changed = True
            sid = str(system.get("id") or "system")
            id_map = {str(child.get("id")): f"{sid}__{child.get('id')}"
                      for child in children if child.get("id")}
            for child in children:
                original = str(child.get("id") or "")
                child["id"] = id_map.get(original, f"{sid}__part")
                child["system_instance"] = sid
                child["system_source"] = str(system.get("source_ref") or "")[:100]
            compiled_edges = []
            for edge in child_edges:
                source = id_map.get(str(edge.get("from")))
                target = id_map.get(str(edge.get("to")))
                if source and target:
                    compiled_edges.append({**edge, "from": source, "to": target})
            executable = {child["id"] for child in children
                          if child.get("type") in EXECUTABLE_NODE_TYPES}
            internal_exec = [edge for edge in compiled_edges
                             if edge.get("from") in executable
                             and edge.get("to") in executable]
            has_in = {edge["to"] for edge in internal_exec}
            has_out = {edge["from"] for edge in internal_exec}
            entries = [node_id for node_id in executable if node_id not in has_in]
            exits = [node_id for node_id in executable if node_id not in has_out]
            next_edges = [edge for edge in edges
                          if edge.get("from") != sid and edge.get("to") != sid]
            for edge in edges:
                if edge.get("to") == sid:
                    next_edges.extend({**edge,
                                       "id": f"{edge.get('id', 'edge')}:{entry}",
                                       "to": entry, "to_port": "input"}
                                      for entry in entries)
                if edge.get("from") == sid:
                    next_edges.extend({**edge,
                                       "id": f"{edge.get('id', 'edge')}:{exit_id}",
                                       "from": exit_id, "from_port": "result"}
                                      for exit_id in exits)
            nodes = [node for node in nodes if node is not system] + children
            edges = next_edges + compiled_edges
        if not changed:
            break
    if any(node.get("type") == "system" for node in nodes):
        raise ValueError("nested Flow instances may be at most three levels deep")
    return nodes, edges


def _compile(payload):
    nodes, edges = _expand_systems(payload.get("nodes") or [],
                                   payload.get("edges") or [])
    if not nodes or len(nodes) > MAX_NODES:
        raise ValueError("a Flow needs 1-80 parts")
    if len(edges) > MAX_EDGES:
        raise ValueError("a Flow may have at most 240 connections")
    exec_nodes = [n for n in nodes if n.get("type") in EXECUTABLE_NODE_TYPES]
    if not exec_nodes:
        raise ValueError("a Flow needs at least one executable part")
    ids = [str(n.get("id") or "").strip() for n in exec_nodes]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("executable part ids must be unique and non-empty")
    known = set(ids)
    by_id = {str(node.get("id") or ""): node for node in nodes}
    contexts = {str(item.get("id") or ""): item
                for item in _clean_contexts(payload.get("contexts"))}
    needs = {sid: [] for sid in ids}
    for edge in edges:
        source, target = edge.get("from"), edge.get("to")
        if source in known and target in known and source not in needs[target]:
            needs[target].append(source)
    stages = []
    incoming = {sid: [] for sid in ids}
    outgoing = {sid: [] for sid in ids}
    graph_incoming = {str(node.get("id") or ""): [] for node in nodes
                      if node.get("id")}
    for edge in edges:
        target = edge.get("to")
        if target in incoming:
            incoming[target].append(edge)
        if target in graph_incoming:
            graph_incoming[target].append(edge)
        source = edge.get("from")
        if source in outgoing:
            outgoing[source].append(edge)

    def routed_sources(connector_id, seen=None):
        """Return tool/context parts feeding a chain of visual connectors."""
        seen = set(seen or ())
        if connector_id in seen:
            return []
        seen.add(connector_id)
        found = []
        for edge in graph_incoming.get(connector_id, []):
            source = by_id.get(str(edge.get("from") or "")) or {}
            if source.get("type") in {"tool", "context"}:
                found.append(source)
            elif source.get("type") == "connector":
                found.extend(routed_sources(str(source.get("id") or ""), seen))
        unique = {}
        for source in found:
            unique[str(source.get("id") or id(source))] = source
        return list(unique.values())
    for node in exec_nodes:
        sid = str(node["id"])
        node_type = node.get("type")
        mode = node_type if node_type in {"judge", "logic", "approval", "output", "native"} \
            else circuits.norm_stage_mode(node.get("mode"), "manual")
        stage = {
            "id": sid,
            "name": str(node.get("name") or sid).strip()[:200],
            "model": str(node.get("model") or "").strip()[:80],
            "mode": mode,
            "needs": needs[sid],
        }
        if mode == "judge":
            judge = dict(node.get("judge") or {})
            judge.setdefault("of", needs[sid])
            stage["judge"] = judge
        elif mode == "logic":
            logic = dict(node.get("logic") or {})
            logic["operation"] = str(logic.get("operation") or "always")[:40]
            logic["value"] = str(logic.get("value") or "")[:2000]
            stage["logic"] = logic
        elif mode == "approval":
            approval = dict(node.get("approval") or {})
            approval["instructions"] = str(
                approval.get("instructions") or node.get("prompt")
                or node.get("description") or "Approval required")[:4000]
            stage["approval"] = approval
        elif mode == "output":
            output = dict(node.get("output") or {})
            output["destination"] = str(
                output.get("destination") or "record")[:80]
            output["instructions"] = str(
                output.get("instructions") or node.get("prompt") or "")[:4000]
            stage["output"] = output
        elif mode == "native":
            routine_id = str(node.get("routine_id") or node.get("source_ref") or "")[:100]
            if not routine_id:
                raise ValueError(f"part {sid}: native system has no routine source")
            stage["native"] = {"routine_id": routine_id}
        else:
            prompt = str(node.get("prompt") or "").strip()[:MAX_TEXT]
            if not prompt:
                raise ValueError(f"part {sid}: instruction prompt is empty")
            stage["prompt"] = prompt
            stage["read_only"] = bool(node.get("read_only"))
            if node.get("extra"):
                stage["extra"] = str(node.get("extra"))[:4000]
        attachments = {"contexts": [], "tools": [], "wire_instructions": []}
        if node.get("system_instance"):
            attachments["system"] = {
                "instance": str(node.get("system_instance"))[:100],
                "source": str(node.get("system_source") or "")[:100],
            }
        for edge in incoming[sid]:
            source = by_id.get(str(edge.get("from") or "")) or {}
            source_type = source.get("type")
            instructions = str(edge.get("instructions") or "").strip()[:2000]
            if instructions:
                attachments["wire_instructions"].append(instructions)
            if edge.get("direction") == "both":
                attachments["wire_instructions"].append(
                    "This is a shared-reference connection: use the input and "
                    "return any proposed updates clearly in this stage's output.")
            if source_type == "context":
                saved_context = contexts.get(str(source.get("id") or ""), {})
                attachments["contexts"].append({
                    "name": str(source.get("name") or saved_context.get("name")
                                or "Context")[:200],
                    "ref": str(source.get("source_ref") or saved_context.get("ref")
                               or "")[:1000],
                    "note": str(source.get("description") or saved_context.get("note")
                                or "")[:2000],
                })
            elif source_type == "tool":
                attachments["tools"].append({
                    "name": str(source.get("name") or "Capability")[:200],
                    "source": str(source.get("source") or "")[:80],
                    "source_ref": str(source.get("source_ref") or "")[:1000],
                    "instructions": str(source.get("prompt")
                                        or source.get("description") or "")[:2000],
                })
            elif source_type == "connector":
                for routed in routed_sources(str(source.get("id") or "")):
                    if routed.get("type") == "context":
                        saved_context = contexts.get(str(routed.get("id") or ""), {})
                        attachments["contexts"].append({
                            "name": str(routed.get("name") or saved_context.get("name")
                                        or "Context")[:200],
                            "ref": str(routed.get("source_ref") or saved_context.get("ref")
                                       or "")[:1000],
                            "note": str(routed.get("description") or saved_context.get("note")
                                        or "")[:2000],
                        })
                    elif routed.get("type") == "tool":
                        attachments["tools"].append({
                            "name": str(routed.get("name") or "Capability")[:200],
                            "source": str(routed.get("source") or "")[:80],
                            "source_ref": str(routed.get("source_ref") or "")[:1000],
                            "instructions": str(routed.get("prompt")
                                                or routed.get("description") or "")[:2000],
                        })
        attached_outputs = []
        for edge in outgoing[sid]:
            target = by_id.get(str(edge.get("to") or "")) or {}
            if target.get("type") != "output":
                continue
            config = target.get("output") or {}
            attached_outputs.append({
                "name": str(target.get("name") or "Output")[:200],
                "destination": str(config.get("destination") or "record")[:80],
                "instructions": str(config.get("instructions")
                                    or target.get("prompt") or "")[:2000],
            })
        if attached_outputs:
            attachments["outputs"] = attached_outputs
        if any(attachments.values()):
            stage["forge"] = attachments
        stages.append(stage)
    circuits.validate_stages(stages)
    return stages


def save_flow(payload, save_as=False):
    """Save a circuit-backed Flow and its editor sidecar."""
    if ((payload.get("kind") == "native"
         or str(payload.get("id") or "").startswith("routine:")) and not save_as):
        raise ValueError("native Vira systems are edited through their schedule and source parts")
    stages = _compile(payload)
    source_id = "" if save_as else str(payload.get("id") or "").strip()
    rec = circuits.save_circuit({
        "id": source_id or None,
        "name": str(payload.get("name") or "Untitled Flow").strip()[:200],
        "description": str(payload.get("description") or "").strip()[:2000],
        "stages": stages,
    })
    clean_nodes = []
    for node in payload.get("nodes") or []:
        if node.get("type") not in DISPLAY_NODE_TYPES:
            continue
        clean = {
            "id": str(node.get("id") or "")[:100],
            "type": str(node.get("type") or "")[:40],
            "x": int(node.get("x") or 0), "y": int(node.get("y") or 0),
            "width": max(160, min(int(node.get("width") or 244), 1200)),
            "height": max(90, min(int(node.get("height") or 148), 1000)),
            "expanded": bool(node.get("expanded")),
        }
        if node.get("type") not in AGENT_NODE_TYPES:
            clean.update({
                "name": str(node.get("name") or node.get("type") or "Part")[:200],
                "description": str(node.get("description") or "")[:2000],
                "source": str(node.get("source") or "forge")[:80],
                "source_ref": str(node.get("source_ref") or "")[:1000],
                "prompt": str(node.get("prompt") or "")[:MAX_SOURCE],
                "model": str(node.get("model") or "")[:80],
                "mode": str(node.get("mode") or "")[:80],
                "locked": bool(node.get("locked")),
                "routine_id": str(node.get("routine_id") or "")[:100],
                "daily_at": str(node.get("daily_at") or "")[:5],
                "every_hours": node.get("every_hours"),
                "enabled": bool(node.get("enabled")),
                "notify": bool(node.get("notify")),
                "logic": dict(node.get("logic") or {}),
                "approval": dict(node.get("approval") or {}),
                "output": dict(node.get("output") or {}),
                "source_revision": int(node.get("source_revision") or 1),
                "embedded": dict(node.get("embedded") or {}),
                "source_files": list(node.get("source_files") or [])[:100],
                "source_truncated": bool(node.get("source_truncated")),
            })
            if node.get("type") == "connector":
                clean.update({
                    "connector_mode": str(
                        node.get("connector_mode") or "through")[:20],
                    "input_ports": [str(value)[:40] for value in
                                    list(node.get("input_ports") or [])[:8]],
                    "output_ports": [str(value)[:40] for value in
                                     list(node.get("output_ports") or [])[:8]],
                    "data_kind": str(node.get("data_kind") or "data")[:40],
                })
        clean_nodes.append(clean)
    clean_edges = [_edge(str(e.get("from") or ""), str(e.get("to") or ""), e)
                   for e in payload.get("edges") or []]
    previous = _store()["graphs"].get(rec["id"]) or {}
    graph = {
        "revision": int(previous.get("revision") or 0) + 1,
        "nodes": clean_nodes,
        "edges": clean_edges,
        "contexts": _clean_contexts(payload.get("contexts")),
        "updated": _now(),
    }

    def mutate(state):
        state.setdefault("graphs", {})[rec["id"]] = graph
    jsonstore.mutate(STORE, mutate, _blank(), indent=1, ensure_ascii=False)
    for node in payload.get("nodes") or []:
        if node.get("type") != "trigger":
            continue
        daily_at = str(node.get("daily_at") or "").strip()
        every_hours = node.get("every_hours")
        if not daily_at and not every_hours:
            continue
        routine_data = {
            "name": str(node.get("name") or f"{rec['name']} schedule")[:200],
            "kind": "circuit", "circuit_id": rec["id"],
            "description": str(node.get("description") or "")[:2000],
            "daily_at": daily_at or None,
            "every_hours": every_hours if not daily_at else None,
            "enabled": bool(node.get("enabled", True)),
            "notify": bool(node.get("notify")),
        }
        routine_id = None if save_as else str(node.get("routine_id") or "") or None
        routines.save_routine(routine_data, rid=routine_id)
    return get_flow(rec["id"])


def run_flow(flow_id, input_text, cwd=None, notify=False, output=""):
    if flow_id.startswith("routine:"):
        row = routines.get_routine(flow_id.split(":", 1)[1])
        if not row:
            raise KeyError(flow_id)
        return routines.dispatch(row)
    return circuits.start_run(flow_id, input_text, cwd=cwd, notify=notify,
                              source="forge", flow_options={"output": output})


def kit_source(ref):
    """Read one Kit source safely, never outside the local Claude library."""
    root = LIBRARY.expanduser().resolve()
    path = Path(str(ref or "")).expanduser().resolve()
    if path != root and root not in path.parents:
        raise ValueError("Kit source is outside the local library")
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            raise ValueError("Kit source is not a readable text file")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"ref": str(path), "files": [path.name],
                "text": text[:MAX_SOURCE], "truncated": len(text) > MAX_SOURCE}
    chunks = []
    files = []
    size = 0
    candidates = sorted((item for item in path.rglob("*")
                         if item.is_file() and not any(part.startswith(".")
                                                       for part in item.relative_to(path).parts)
                         and item.suffix.lower() in SOURCE_SUFFIXES),
                        key=lambda item: str(item.relative_to(path)).lower())
    for item in candidates[:80]:
        relative = str(item.relative_to(path))
        text = item.read_text(encoding="utf-8", errors="replace")
        header = f"\n\n--- {relative} ---\n"
        remaining = MAX_SOURCE - size - len(header)
        if remaining <= 0:
            break
        chunk = header + text[:remaining]
        chunks.append(chunk)
        files.append(relative)
        size += len(chunk)
        if len(text) > remaining:
            break
    truncated = len(candidates) > len(files) or size >= MAX_SOURCE
    return {"ref": str(path), "files": files,
            "text": "".join(chunks).lstrip(), "truncated": truncated}


def native_source(routine_id):
    row = routines.get_routine(routine_id)
    if not row:
        raise KeyError(routine_id)
    from . import routinesrc
    return routinesrc.hood(row)


def kit_catalog():
    items = []
    for item in actions.scan_library():
        if item["kind"] == "skill":
            source_ref = LIBRARY / "skills" / item["name"] / "SKILL.md"
        else:
            source_ref = LIBRARY / "commands" / f"{item['name']}.md"
        items.append({**item, "id": f"{item['kind']}:{item['name']}",
                      "type": "tool", "source_ref": str(source_ref)})
    roots = [
        ("script", LIBRARY / "scripts", {".py", ".sh", ".js", ".zsh"}),
        ("agent", LIBRARY / "agents", {".md"}),
        ("mcp", LIBRARY / "mcp-servers", None),
        ("plugin", LIBRARY / "dist-plugins", None),
        ("scheduled-task", LIBRARY / "scheduled-tasks", None),
        ("harness", LIBRARY / "harnesses", None),
        ("hook", LIBRARY / "hooks", None),
    ]
    seen = {item["id"] for item in items}
    for kind, root, suffixes in roots:
        if not root.exists():
            continue
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        for path in children:
            if path.name.startswith("."):
                continue
            if suffixes is not None and (not path.is_file() or path.suffix not in suffixes):
                continue
            if suffixes is None and not path.is_dir():
                continue
            name = path.stem if path.is_file() else path.name
            item_id = f"{kind}:{name}"
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append({
                "id": item_id, "name": name, "kind": kind,
                "type": "agent" if kind == "agent" else "tool",
                "invoke": str(path), "source_ref": str(path),
                "description": f"{kind.replace('_', ' ').title()} capability",
                "arg_fields": [],
            })
    primitives = [
        ("trigger", "trigger", "Trigger", "Manual, scheduled, or event start", {}),
        ("context", "context", "Context", "A reference set from Find or Reader", {}),
        ("agent", "agent", "Agent", "Model, permission, prompt, and capabilities", {}),
        ("logic", "logic", "Logic", "Branch, merge, judge, or retry", {}),
        ("approval", "approval", "Approval", "Pause for an explicit human decision", {}),
        ("output", "output", "Output", "Artifact, record, notification, or destination", {}),
        ("connector", "connector", "Connector", "Configurable input/output adapter", {
            "connector_mode": "through", "input_ports": ["in"],
            "output_ports": ["out"], "data_kind": "data",
        }),
        ("input-port", "connector", "Input port", "A source chip that introduces data to a Flow", {
            "connector_mode": "input", "input_ports": [],
            "output_ports": ["out"], "data_kind": "data",
        }),
        ("output-port", "connector", "Output port", "A destination chip at the edge of a Flow", {
            "connector_mode": "output", "input_ports": ["in"],
            "output_ports": [], "data_kind": "data",
        }),
        ("tool-bus", "connector", "Tool bus", "Expandable capability bus for tools, skills, scripts, and MCP", {
            "connector_mode": "through", "input_ports": ["tool"],
            "output_ports": ["capability"], "data_kind": "capability",
        }),
    ]
    for item_id, kind, name, description, defaults in primitives:
        items.insert(0, {"id": f"primitive:{item_id}", "name": name,
                         "kind": "primitive", "type": kind,
                         "invoke": "", "description": description,
                         "arg_fields": [], **defaults})
    return items
