"""Read-only adapters for local, evidence-backed research graphs.

The SQLite database is the canonical research record.  Its manifest is build
metadata; reading rooms and vault notes are projections; an application bridge
is personal, local interpretation.  Keeping those layers named and separate is
the point of this module: callers should never accidentally present a room
annotation or a personal fit note as source evidence.

Graphs may be configured with ``research_graphs`` or discovered beneath the
self-record at ``*/corpus/data/manifest.json``.  This module deliberately has
no write path and opens every database in SQLite ``mode=ro`` with
``query_only`` enabled.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import applications, readingroom, roomvault, settings


class ResearchGraphError(RuntimeError):
    """A graph is configured or present but cannot be read."""


_SPEC_KEYS = {
    "id", "name", "company", "manifest", "database", "path", "room",
    "taxonomy",
}
_TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _slug(value):
    value = re.sub(r"^\d+[-_ ]*", "", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "research"


def _path_spec(raw, graph_id=None):
    """Normalize one permissive config/discovery entry."""
    spec = dict(raw) if isinstance(raw, dict) else {"path": raw}
    if graph_id and not spec.get("id"):
        spec["id"] = graph_id
    base = spec.get("path")
    if base:
        path = Path(str(base)).expanduser()
        if path.suffix.lower() == ".json":
            spec.setdefault("manifest", path)
        elif path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            spec.setdefault("database", path)
        else:
            candidates = (
                path / "manifest.json",
                path / "data" / "manifest.json",
                path / "corpus" / "data" / "manifest.json",
            )
            manifest = next((p for p in candidates if p.is_file()), None)
            if manifest:
                spec.setdefault("manifest", manifest)
    manifest_path = spec.get("manifest")
    manifest_path = Path(str(manifest_path)).expanduser() if manifest_path else None
    manifest = _read_json(manifest_path, {}) if manifest_path else {}
    manifest = manifest if isinstance(manifest, dict) else {}

    database = spec.get("database") or manifest.get("database")
    if database:
        database = Path(str(database)).expanduser()
        if not database.is_absolute() and manifest_path:
            database = manifest_path.parent / database
    elif manifest_path:
        databases = sorted(
            p for pattern in ("*.sqlite", "*.sqlite3", "*.db")
            for p in manifest_path.parent.glob(pattern)
        )
        database = databases[0] if len(databases) == 1 else None

    subject = manifest_path.parent.parent.parent if manifest_path else None
    inferred = subject.name if subject else (database.stem if database else "research")
    gid = _slug(spec.get("id") or inferred)
    corpus = manifest_path.parent.parent if manifest_path else None
    taxonomy = spec.get("taxonomy")
    if taxonomy:
        taxonomy = Path(str(taxonomy)).expanduser()
    elif corpus:
        taxonomy = corpus / "application_bridge_taxonomy.json"

    error = ""
    if manifest_path and not manifest_path.is_file():
        error = "manifest missing"
    elif not database:
        error = "database not identified"
    elif not database.is_file():
        error = "database missing"
    room = str(spec.get("room") or gid)
    if not spec.get("room") and readingroom.load_room(room) is None:
        universe_room = f"{gid}-universe"
        if readingroom.load_room(universe_room) is not None:
            room = universe_room
    return {
        "id": gid,
        "name": str(spec.get("name") or manifest.get("name") or
                    gid.replace("-", " ").title()),
        "company": str(spec.get("company") or manifest.get("company") or
                       gid.replace("-", " ").title()),
        "manifest_path": manifest_path,
        "database_path": database,
        "taxonomy_path": taxonomy,
        "room": room,
        "manifest": manifest,
        "error": error,
    }


def _configured_specs(config):
    if isinstance(config, (str, Path)):
        return [(None, config)]
    if isinstance(config, list):
        return [(None, value) for value in config]
    if not isinstance(config, dict):
        return []
    if set(config) & _SPEC_KEYS:
        return [(None, config)]
    return [(str(key), value) for key, value in config.items()]


def _graphs():
    raw = settings.raw()
    configured = isinstance(raw, dict) and "research_graphs" in raw
    if configured:
        entries = _configured_specs(raw.get("research_graphs"))
    else:
        try:
            entries = [(None, path) for path in sorted(
                applications.self_record().glob("*/corpus/data/manifest.json")
            )]
        except OSError:
            entries = []
    out = []
    seen = set()
    for graph_id, value in entries:
        record = _path_spec(value, graph_id)
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        out.append(record)
    return out


def _public_graph(graph):
    manifest = graph["manifest"]
    counts = manifest.get("tables", {})
    return {
        "id": graph["id"],
        "name": graph["name"],
        "company": graph["company"],
        "status": "error" if graph["error"] else "ready",
        "error": graph["error"],
        "database": str(graph["database_path"] or ""),
        "manifest": str(graph["manifest_path"] or ""),
        "room": graph["room"],
        "built": manifest.get("built"),
        "manifest_counts": counts,
        "counts": {
            "sources": counts.get("sources", 0),
            "events": counts.get("events", 0),
            "utterances": counts.get("utterances", 0),
            "claims": counts.get("claims", 0),
        },
        "claim_count": counts.get("claims", 0),
        "taxonomy_present": bool(
            graph["taxonomy_path"] and graph["taxonomy_path"].is_file()
        ),
        "authority": {
            "database": "canonical",
            "manifest": "build_metadata",
            "room": "linked_projection",
            "vault": "linked_projection",
            "application_bridge": "personal_local",
        },
    }


def catalog():
    """Return every configured/discovered graph without opening its database."""
    return [_public_graph(graph) for graph in _graphs()]


def _graph(graph_id=None):
    graphs = _graphs()
    if not graphs:
        raise ResearchGraphError("no research graphs are configured or discovered")
    if graph_id is None:
        if len(graphs) != 1:
            raise ResearchGraphError("graph_id is required when multiple graphs exist")
        graph = graphs[0]
    else:
        wanted = str(graph_id).casefold()
        graph = next((g for g in graphs if g["id"].casefold() == wanted), None)
        if graph is None:
            raise ResearchGraphError(f"unknown research graph: {graph_id}")
    if graph["error"]:
        raise ResearchGraphError(f"{graph['id']}: {graph['error']}")
    return graph


def _connect(graph):
    try:
        con = sqlite3.connect(
            f"file:{graph['database_path']}?mode=ro", uri=True
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        return con
    except sqlite3.Error as exc:
        raise ResearchGraphError(f"cannot open {graph['id']} read-only: {exc}") from exc


def _tables(con):
    return {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _rows(con, sql, params=()):
    try:
        return [dict(row) for row in con.execute(sql, params)]
    except sqlite3.Error:
        return []


def _row(con, sql, params=()):
    rows = _rows(con, sql, params)
    return rows[0] if rows else None


def _count(con, table):
    # table is always sourced from sqlite_master, never user input.
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return 0


def overview(graph_id=None, claim_limit=200):
    """Summarize a graph from canonical rows, plus named build metadata."""
    graph = _graph(graph_id)
    with closing(_connect(graph)) as con:
        tables = _tables(con)
        counts = {name: _count(con, name) for name in sorted(tables)}
        claims = []
        if "claim_rollups" in tables:
            claims = _rows(
                con,
                "SELECT * FROM claim_rollups "
                "ORDER BY personal_relevance_score DESC, claim_id LIMIT ?",
                (max(0, int(claim_limit)),),
            )
        elif "claims" in tables:
            claims = _rows(
                con,
                "SELECT * FROM claims ORDER BY personal_relevance_score DESC, "
                "claim_id LIMIT ?",
                (max(0, int(claim_limit)),),
            )
        latest = None
        if "sources" in tables:
            latest = _row(
                con,
                "SELECT publication_date, source_id, title FROM sources "
                "WHERE publication_date IS NOT NULL AND publication_date != '' "
                "ORDER BY publication_date DESC LIMIT 1",
            )
    manifest = graph["manifest"]
    public = _public_graph(graph)
    compact_counts = {
        "sources": counts.get("sources", 0),
        "events": counts.get("events", 0),
        "utterances": counts.get("utterances", 0),
        "claims": counts.get("claims", 0),
    }
    return {
        "graph": public,
        # Stable display aliases keep the browser client thin while the
        # named authority layers below remain the contract of record.
        "project": {
            **public,
            "title": public["name"],
            "subtitle": "Claim-first public language with event-scoped evidence",
            "counts": compact_counts,
        },
        "claims": claims,
        "counts": compact_counts,
        "canonical": {
            "authority": "database",
            "table_counts": counts,
            "latest_source": latest,
            "claim_summaries": claims,
        },
        "build_metadata": {
            "authority": "manifest",
            "built": manifest.get("built"),
            "declared_table_counts": manifest.get("tables", {}),
            "source_layers": manifest.get("source_layers"),
            "capture_layers": manifest.get("capture_layers"),
            "analysis_scopes": manifest.get("analysis_scopes"),
            "limitations": manifest.get("limitations"),
        },
    }


def claim_detail(claim_id_or_label, graph_id=None, limit=50):
    """Return one claim and its canonical evidence, without projection data."""
    graph = _graph(graph_id)
    cap = max(0, int(limit))
    with closing(_connect(graph)) as con:
        tables = _tables(con)
        if "claims" not in tables:
            raise ResearchGraphError(f"{graph['id']} has no claims table")
        claim = _row(
            con,
            "SELECT * FROM claims WHERE claim_id = ? OR "
            "lower(claim_label) = lower(?) LIMIT 1",
            (claim_id_or_label, claim_id_or_label),
        )
        if not claim:
            return None
        rollup = None
        if "claim_rollups" in tables:
            rollup = _row(
                con, "SELECT * FROM claim_rollups WHERE claim_id = ?",
                (claim["claim_id"],),
            )
        total = 0
        evidence = []
        if "claim_utterances" in tables:
            total_row = _row(
                con, "SELECT COUNT(*) AS n FROM claim_utterances WHERE claim_id = ?",
                (claim["claim_id"],),
            )
            total = (total_row or {}).get("n", 0)
            evidence = _rows(
                con,
                "SELECT * FROM claim_utterances WHERE claim_id = ? "
                "ORDER BY event_id, utterance_id LIMIT ?",
                (claim["claim_id"], cap),
            )
            if "utterance_appearances" in tables:
                for item in evidence:
                    item["appearances"] = _rows(
                        con,
                        "SELECT * FROM utterance_appearances WHERE utterance_id = ? "
                        "ORDER BY source_id, appearance_id",
                        (item.get("utterance_id"),),
                    )
    vault_notes = _claim_vault_notes(graph, claim)
    application_bridges = _claim_application_bridges(graph, claim)
    return {
        "graph": _public_graph(graph),
        "authority": "database",
        "claim": claim,
        "rollup": rollup,
        "evidence": evidence,
        "vault_notes": vault_notes,
        "application_bridges": application_bridges,
        "evidence_total": total,
        "evidence_returned": len(evidence),
        "truncated": total > len(evidence),
    }


def _claim_vault_notes(graph, claim):
    """Resolve a generated public claim projection without making it canonical."""
    root = Path(str(settings.get("vault_root") or "")).expanduser()
    if not root.is_dir():
        return []
    label = _slug(claim.get("claim_label") or claim.get("claim_id"))[:96]
    rel = Path("wiki") / f"{graph['id']}-claim-{label}.md"
    return ([{"path": rel.as_posix(), "title": claim.get("claim_label"),
              "authority": "linked_projection"}]
            if (root / rel).is_file() else [])


def _claim_application_bridges(graph, claim):
    """Return category-level application uses, never personal claims or evidence."""
    taxonomy = (_read_json(graph.get("taxonomy_path"), {})
                if graph.get("taxonomy_path") else {})
    items = taxonomy.get("items", []) if isinstance(taxonomy, dict) else []
    category = str(claim.get("category") or "").casefold()
    claim_id = str(claim.get("claim_id") or "")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapped = [str(value) for value in item.get("claim_ids") or []]
        if mapped and claim_id not in mapped:
            continue
        if not mapped and str(item.get("group") or "").casefold() != category:
            continue
        out.append({
            "id": item.get("id"),
            "title": item.get("label"),
            "relevance": item.get("application_use"),
            "rank": item.get("rank"),
            "scope": "personal_local_category_bridge",
        })
    return sorted(out, key=lambda item: (item.get("rank") or 999, item.get("id") or ""))


def _normalize_url(url):
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return str(url).strip().lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port else ""
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    path = (parts.path or "/").rstrip("/") or "/"
    return urlunsplit(("", host + port, path, urlencode(query), ""))


def _source_urls(con, source):
    urls = {
        _normalize_url(source.get(key))
        for key in ("original_url", "canonical_url") if source.get(key)
    }
    if "source_aliases" in _tables(con):
        for alias in _rows(
            con, "SELECT * FROM source_aliases WHERE source_id = ?",
            (source.get("source_id"),),
        ):
            for key, value in alias.items():
                if "url" in key and value:
                    urls.add(_normalize_url(value))
            if (str(alias.get("alias_type") or "").lower().endswith("url")
                    and alias.get("alias_value")):
                urls.add(_normalize_url(alias["alias_value"]))
    return {url for url in urls if url}


def _vault_projection(slug, item):
    projected = copy.deepcopy(item)
    owner = str(projected.get("vault") or "").strip()
    if owner:
        return {"authority": "linked_projection", "note": owner, "kind": "owner"}
    try:
        roomvault.resolve(slug, [projected])
    except (OSError, RuntimeError, ValueError):
        return {"authority": "linked_projection", "note": "", "kind": ""}
    return {
        "authority": "linked_projection",
        "note": projected.get("vault_note", ""),
        "kind": projected.get("vault_note_kind", ""),
    }


def _room_matches(urls, preferred=None):
    rooms = readingroom.list_rooms()
    if preferred:
        rooms.sort(key=lambda room: room.get("slug") != preferred)
    matches = []
    for summary in rooms:
        slug = summary.get("slug")
        room = readingroom.load_room(slug)
        if not room:
            continue
        for item in room.get("items", []):
            if _normalize_url(item.get("url")) in urls:
                matches.append({
                    "authority": "linked_projection",
                    "room": {k: room.get(k) for k in
                             ("slug", "title", "subtitle", "built", "updated")},
                    "item": copy.deepcopy(item),
                    "vault": _vault_projection(slug, item),
                })
    return matches


def source_detail(source_id, graph_id=None, limit=100):
    """Return one canonical source record with separately named projections."""
    graph = _graph(graph_id)
    cap = max(0, int(limit))
    with closing(_connect(graph)) as con:
        tables = _tables(con)
        if "sources" not in tables:
            raise ResearchGraphError(f"{graph['id']} has no sources table")
        source = _row(con, "SELECT * FROM sources WHERE source_id = ?", (source_id,))
        if not source:
            return None
        urls = _source_urls(con, source)
        captures = (_rows(con, "SELECT * FROM captures WHERE source_id = ? LIMIT ?",
                          (source_id, cap)) if "captures" in tables else [])
        events = (_rows(
            con,
            "SELECT e.*, es.source_role, es.relationship, es.confidence, "
            "es.is_canonical FROM event_sources es JOIN events e "
            "ON e.event_id = es.event_id WHERE es.source_id = ? LIMIT ?",
            (source_id, cap),
        ) if {"events", "event_sources"} <= tables else [])
        appearances = (_rows(
            con, "SELECT * FROM utterance_appearances WHERE source_id = ? LIMIT ?",
            (source_id, cap),
        ) if "utterance_appearances" in tables else [])
        claims = []
        if {"claims", "claim_utterances", "utterance_appearances"} <= tables:
            claims = _rows(
                con,
                "SELECT DISTINCT c.* FROM claims c JOIN claim_utterances cu "
                "ON cu.claim_id = c.claim_id JOIN utterance_appearances ua "
                "ON ua.utterance_id = cu.utterance_id WHERE ua.source_id = ? LIMIT ?",
                (source_id, cap),
            )
        relations = (_rows(
            con,
            "SELECT * FROM source_relations WHERE source_id = ? OR related_source_id = ? "
            "LIMIT ?",
            (source_id, source_id, cap),
        ) if "source_relations" in tables else [])
    room_matches = _room_matches(urls, graph["room"])
    return {
        "graph": _public_graph(graph),
        # Display aliases; canonical/projections below preserve provenance.
        "source": source,
        "captures": captures,
        "events": events,
        "appearances": appearances,
        "claims": claims,
        "relations": relations,
        "vault_notes": [{
            "path": match["vault"]["note"],
            "title": Path(match["vault"]["note"]).stem.replace("-", " "),
            "authority": "linked_projection",
        } for match in room_matches if match.get("vault", {}).get("note")],
        "canonical": {
            "authority": "database",
            "source": source,
            "captures": captures,
            "events": events,
            "appearances": appearances,
            "claims": claims,
            "relations": relations,
        },
        "projections": {
            "authority": "linked_projection",
            "reading_rooms": room_matches,
        },
    }


def room_annotation(room_slug, item_id=None, source_id=None, graph_id=None):
    """Map a room item to a canonical graph source without modifying either."""
    room = readingroom.load_room(room_slug)
    if not room:
        return None
    graph = _graph(graph_id)
    with closing(_connect(graph)) as con:
        tables = _tables(con)
        wanted_source = None
        if source_id and "sources" in tables:
            wanted_source = _row(
                con, "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            )
        wanted_urls = _source_urls(con, wanted_source) if wanted_source else set()
        item = None
        for candidate in room.get("items", []):
            candidate_id = candidate.get("id") or readingroom.item_id(candidate)
            if item_id and candidate_id == item_id:
                item = candidate
                break
            if wanted_urls and _normalize_url(candidate.get("url")) in wanted_urls:
                item = candidate
                break
        if item is None and not item_id and not source_id and len(room.get("items", [])) == 1:
            item = room["items"][0]
        if item is None:
            return None
        source = wanted_source
        if source is None and "sources" in tables:
            target = _normalize_url(item.get("url"))
            for candidate in _rows(con, "SELECT * FROM sources"):
                if target and target in _source_urls(con, candidate):
                    source = candidate
                    break
    return {
        "room_projection": {
            "authority": "linked_projection",
            "room": {k: room.get(k) for k in
                     ("slug", "title", "subtitle", "built", "updated")},
            "item": copy.deepcopy(item),
        },
        "graph_annotation": {
            "authority": "database",
            "graph_id": graph["id"],
            "source": source,
        },
        "vault_projection": _vault_projection(room_slug, item),
    }


def company_lookup(company):
    """Find a graph by company, display name, or stable id."""
    wanted = str(company or "").strip().casefold()
    if not wanted:
        return None
    rows = catalog()
    for row in rows:
        if wanted in {row["id"].casefold(), row["name"].casefold(),
                      row["company"].casefold()}:
            return row
    return next((row for row in rows if any(
        wanted in value.casefold() for value in
        (row["id"], row["name"], row["company"])
    )), None)


def application_context(uid=None, role=None, company=None, graph_id=None,
                        claim_limit=20):
    """Join a role to research while preserving the authority boundaries."""
    role = copy.deepcopy(role) if isinstance(role, dict) else (
        applications.find_role(uid) if uid else None
    )
    company = company or (role or {}).get("company") or (role or {}).get("org")
    if graph_id is None:
        match = company_lookup(company) if company else None
        graph_id = match["id"] if match else None
    summary = overview(graph_id, claim_limit=claim_limit)
    graph = _graph(graph_id)
    taxonomy = _read_json(graph["taxonomy_path"], None) if graph["taxonomy_path"] else None
    return {
        "role": role,
        "company": company,
        "research": summary,
        "personal_bridge": {
            "scope": "personal_local",
            "canonical": False,
            "status": "available" if taxonomy is not None else "absent",
            "path": str(graph["taxonomy_path"] or ""),
            "taxonomy": taxonomy,
        },
    }
