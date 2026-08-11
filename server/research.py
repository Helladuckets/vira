"""Read-only adapters for local, evidence-backed research graphs.

The SQLite database is the canonical research record.  Its manifest is build
metadata; reading rooms and vault notes are projections; an application bridge
is personal, local interpretation.  Keeping those layers named and separate is
the point of this module: callers should never accidentally present a room
annotation or a personal fit note as source evidence.

Graphs may be configured with ``research_graphs`` or discovered beneath the
self-record at ``*/corpus/data/manifest.json`` and one level deeper
(``*/*/corpus/data/manifest.json``) -- a corpus filed under a layer such as
``pipelines/`` is the same corpus.  This module deliberately has no write path
and opens every database in SQLite ``mode=ro`` with ``query_only`` enabled.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import applications, fullingest, readingroom, roomvault, settings


class ResearchGraphError(RuntimeError):
    """A graph is configured or present but cannot be read."""


# Where an unconfigured graph is looked for, relative to the self-record.  Two
# depths, not a walk: a corpus sits either at the record's top level or inside
# one named layer (``pipelines/anthropic-corpus/corpus/...``).  An rglob over a
# record holding decades of evidence and rendering archives would be the wrong
# cost for a lookup this shallow.
DISCOVERY_GLOBS = (
    "*/corpus/data/manifest.json",
    "*/*/corpus/data/manifest.json",
)
_CORPUS_SUFFIX = re.compile(r"[-_ ]corpus$", re.I)

_SPEC_KEYS = {
    "id", "name", "company", "manifest", "database", "path", "room",
    "taxonomy",
}
_TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}
_ORGANIZATION_VOICE_SCOPES = {
    "company_first_party",
    "anthropic_person_direct",
}
_DISTRIBUTION_RELATIONS = {
    "clip", "distribution", "excerpt", "excerpt_or_compilation",
    "full_repost", "mirror", "partial_repost", "related_manifestation",
    "repost", "secondary_coverage", "syndication",
}
_SENSITIVE_QUERY_KEYS = {
    "email", "email_address", "first_name", "firstname", "fname",
    "full_name", "fullname", "last_name", "lastname", "lname", "name",
}
_LOCAL_PATH_TEXT = re.compile(
    r"(?<![A-Za-z0-9.])(?:file://)?/(?:Users|home)/[^/\s\"'<>]+(?:/[^\s\"'<>]*)*"
    r"|\b[A-Za-z]:\\Users\\[^\\\s\"'<>]+(?:\\[^\s\"'<>]*)*",
    re.I,
)
_RESEARCH_QUESTION_WORDS = {
    "anthropic", "claim", "claims", "employee", "employees", "evidence",
    "interview", "interviews", "podcast", "podcasts", "quote", "quotes",
    "speaker", "speakers", "source", "sources", "transcript", "transcripts",
}
_CLAIM_QUERY_STOPWORDS = _RESEARCH_QUESTION_WORDS | {
    "a", "about", "all", "also", "an", "and", "are", "as", "at", "be",
    "by", "defined", "defines", "did", "do", "does", "else", "explain",
    "explained", "find", "for", "from", "has", "have", "how", "i", "in",
    "is", "it", "me", "of", "on", "or", "said", "say", "show", "that",
    "the", "their", "them", "this", "to", "was", "what", "where", "who",
    "with", "would", "you",
}
_EXPLANATION_MARKERS = re.compile(
    r"\b(?:because|means?|matters?|important|value|valuable|skill|principle|"
    r"approach|reason|therefore|so that|in other words|if you|lets? you|"
    r"allows? you|helps? you|figure out|deduce|optimizing for)\b",
    re.I,
)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _slug(value):
    value = re.sub(r"^\d+[-_ ]*", "", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "research"


def _subject_name(name):
    """The subject a corpus directory is ABOUT, from its directory name.

    A directory holding a ``corpus/`` subtree is conventionally named
    ``<subject>-corpus``, so that suffix names the layer, not the subject -- and
    it must not reach the graph id, the displayed company, or the reading-room
    slug the graph links itself to.  An explicit ``id`` in config always wins
    over this inference; stripping is skipped when it would leave nothing.
    """
    trimmed = _CORPUS_SUFFIX.sub("", str(name or "")).strip()
    return trimmed or name


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
        elif manifest_path and not database.is_file():
            # A manifest records the absolute path the build wrote to, and the
            # manifest is BUILD METADATA while the database is canonical.  Move
            # the corpus directory and that recorded path is stale, so fall back
            # to the same filename beside the manifest -- they travel together.
            beside = manifest_path.parent / database.name
            if beside.is_file():
                database = beside
    elif manifest_path:
        databases = sorted(
            p for pattern in ("*.sqlite", "*.sqlite3", "*.db")
            for p in manifest_path.parent.glob(pattern)
        )
        database = databases[0] if len(databases) == 1 else None

    subject = manifest_path.parent.parent.parent if manifest_path else None
    inferred = (_subject_name(subject.name) if subject
                else (database.stem if database else "research"))
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
            record = applications.self_record()
            found = set()
            for pattern in DISCOVERY_GLOBS:
                found.update(record.glob(pattern))
            entries = [(None, path) for path in sorted(found)]
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
    database = graph.get("database_path")
    manifest_path = graph.get("manifest_path")
    return {
        "id": graph["id"],
        "name": graph["name"],
        "company": graph["company"],
        "status": "error" if graph["error"] else "ready",
        "error": graph["error"],
        "database": database.name if database else "",
        "manifest": manifest_path.name if manifest_path else "",
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


def _claim_taxonomy(graph):
    """The graph builder's definition set, when it lives beside the DB.

    Definitions without evidence are intentionally absent from the canonical
    claims table.  Counting both answers a different question from merely
    counting materialized claims and prevents a partial build looking final.
    """
    manifest = graph.get("manifest_path")
    if not manifest:
        return []
    payload = _read_json(manifest.parent.parent / "claim_taxonomy.json", {})
    rows = payload.get("claims", payload if isinstance(payload, list) else [])
    return [row for row in rows if isinstance(row, dict)]


def _iso_mtime(path):
    try:
        stamp = Path(path).stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _freshness(graph):
    """Name source material that arrived after the graph database build.

    Reading-room raw captures are inputs to this graph's builder.  They can
    land hours after a one-shot graph build; without this comparison Vira
    confidently serves stale recurrence counts until somebody remembers to
    rebuild by hand.
    """
    database = graph.get("database_path")
    try:
        built_mtime = database.stat().st_mtime
    except (AttributeError, OSError):
        return {"status": "unknown", "newer_source_count": 0,
                "newer_sources": []}
    root = Path(str(settings.get("vault_root") or "")).expanduser()
    room = readingroom.load_room(graph.get("room"))
    newer_by_path = {}
    if root.is_dir() and room:
        for item in room.get("items", []):
            try:
                path = fullingest.raw_path(root, item)
                stamp = path.stat().st_mtime
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if stamp <= built_mtime:
                continue
            rel = path.relative_to(root).as_posix()
            row = newer_by_path.setdefault(rel, {
                "item_id": item.get("id"),
                "item_ids": [],
                "title": item.get("title"),
                "path": rel,
                "updated": datetime.fromtimestamp(
                    stamp, timezone.utc).isoformat(),
            })
            if item.get("id") and item.get("id") not in row["item_ids"]:
                row["item_ids"].append(item["id"])
    newer = list(newer_by_path.values())
    newer.sort(key=lambda row: row.get("updated", ""), reverse=True)
    return {
        "status": "stale" if newer else "current",
        "database_updated": _iso_mtime(database),
        "newer_source_count": len(newer),
        # Count every input, but bound the diagnostic sample sent to browsers.
        "newer_sources": newer[:20],
        "newer_sources_returned": min(len(newer), 20),
        "newer_sources_truncated": len(newer) > 20,
    }


def _organization_edge(item):
    if bool(item.get("speaker_verified")):
        return True
    source = item.get("source") or {}
    return str(source.get("voice_scope") or "") in _ORGANIZATION_VOICE_SCOPES


def _scope_rollup(evidence):
    organization = [row for row in evidence if row.get("evidence_scope") ==
                    "organization"]
    contextual = [row for row in evidence if row.get("evidence_scope") ==
                  "context"]
    speakers = {
        str(row.get("speaker_person_id") or row.get("speaker_name") or "")
        for row in organization if row.get("speaker_verified") and
        (row.get("speaker_person_id") or row.get("speaker_name"))
    }
    events = {row.get("event_id") for row in organization if row.get("event_id")}
    appearances = {
        appearance.get("appearance_id")
        for row in organization for appearance in row.get("appearances", [])
        if appearance.get("appearance_id")
    }
    return {
        "distinct_speaker_count": len(speakers),
        "distinct_event_count": len(events),
        "utterance_count": len({row.get("utterance_id") for row in organization
                                if row.get("utterance_id")}),
        "appearance_count": len(appearances),
        "contextual_utterance_count": len({
            row.get("utterance_id") for row in contextual if row.get("utterance_id")
        }),
        "evidence_scope": ("organization" if organization else
                           "context_only" if contextual else "none"),
    }


def _enrich_evidence(con, tables, evidence):
    """Attach the event and real source records claim edges point at."""
    event_cache = {}
    source_cache = {}

    def source(source_id):
        if not source_id or "sources" not in tables:
            return None
        if source_id not in source_cache:
            source_cache[source_id] = _row(
                con, "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            )
        return copy.deepcopy(source_cache[source_id])

    for item in evidence:
        event_id = item.get("event_id")
        if event_id and "events" in tables:
            if event_id not in event_cache:
                event_cache[event_id] = _row(
                    con, "SELECT * FROM events WHERE event_id = ?", (event_id,)
                )
            event = copy.deepcopy(event_cache[event_id])
        else:
            event = None
        item["event"] = event
        if event:
            for key in ("event_title", "event_date", "venue", "publisher"):
                if event.get(key) and not item.get(key):
                    item[key] = event[key]

        event_sources = []
        if event_id and {"event_sources", "sources"} <= tables:
            links = _rows(
                con,
                "SELECT * FROM event_sources WHERE event_id = ? "
                "ORDER BY is_canonical DESC, source_id",
                (event_id,),
            )
            for link in links:
                record = source(link.get("source_id"))
                if not record:
                    continue
                record.update({
                    key: value for key, value in link.items()
                    if key not in record and value not in (None, "")
                })
                event_sources.append(record)
        item["sources"] = event_sources
        canonical_id = (event or {}).get("canonical_source_id")
        canonical = source(canonical_id)
        if canonical is None:
            canonical = next((row for row in event_sources if row.get("is_canonical")),
                             event_sources[0] if event_sources else None)
        item["source"] = canonical
        if event:
            provenance_sources = [canonical] + event_sources
            provenance_sources = [row for row in provenance_sources if row]
            if not event.get("event_date"):
                event["event_date"] = next((
                    str(row.get("publication_date")) for row in provenance_sources
                    if row.get("publication_date")
                ), "Date not published")
                event["event_date_status"] = "source_fallback" if (
                    event["event_date"] != "Date not published"
                ) else "unknown"
            if not event.get("venue"):
                event["venue"] = next((
                    str(row.get(key)) for row in provenance_sources
                    for key in ("venue", "publisher", "title") if row.get(key)
                ), "Venue not published")
                event["venue_status"] = "source_fallback" if (
                    event["venue"] != "Venue not published"
                ) else "unknown"
            for key in ("event_date", "venue", "publisher"):
                if event.get(key) and not item.get(key):
                    item[key] = event[key]

        for appearance in item.get("appearances", []):
            appearance["source"] = source(appearance.get("source_id"))
        item["evidence_scope"] = (
            "organization" if _organization_edge(item) else "context"
        )
    return evidence


def _evidence_sources(item):
    """Canonical source first, followed by unique distribution appearances."""
    out = []
    seen = set()
    for source in [item.get("source"), *(item.get("sources") or [])]:
        source_id = (source or {}).get("source_id")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        out.append(source)
    return out


def _overview_scope(con, tables, claims):
    """Add organization-only counts without rewriting canonical rollups."""
    if "claim_utterances" not in tables:
        return claims
    edges = _rows(con, "SELECT * FROM claim_utterances ORDER BY claim_id")
    by_claim = {}
    for edge in edges:
        if "utterance_appearances" in tables:
            edge["appearances"] = _rows(
                con,
                "SELECT appearance_id FROM utterance_appearances "
                "WHERE utterance_id = ?",
                (edge.get("utterance_id"),),
            )
        by_claim.setdefault(edge.get("claim_id"), []).append(edge)
    for claim_id, rows in by_claim.items():
        _enrich_evidence(con, tables, rows)
    for claim in claims:
        scope = _scope_rollup(by_claim.get(claim.get("claim_id"), []))
        claim["organization_rollup"] = copy.deepcopy(scope)
        claim.update({f"organization_{key}": value for key, value in scope.items()
                      if key.endswith("_count")})
        claim["evidence_scope"] = scope["evidence_scope"]
    return claims


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
        claims = _overview_scope(con, tables, claims)
        materialized_claim_ids = {
            row.get("claim_id") for row in
            (_rows(con, "SELECT claim_id FROM claims") if "claims" in tables else [])
        }
        latest = None
        if "sources" in tables:
            latest = _row(
                con,
                "SELECT publication_date, source_id, title FROM sources "
                "WHERE publication_date IS NOT NULL AND publication_date != '' "
                "ORDER BY publication_date DESC LIMIT 1",
            )
    manifest = graph["manifest"]
    definitions = _claim_taxonomy(graph)
    materialized_ids = materialized_claim_ids
    defined_ids = {
        str(row.get("claim_id") or row.get("id") or "") for row in definitions
        if row.get("claim_id") or row.get("id")
    }
    freshness = _freshness(graph)
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
        "coverage": {
            "materialized_claim_count": counts.get("claims", 0),
            "defined_claim_count": len(defined_ids),
            "unmaterialized_claim_count": len(defined_ids - materialized_ids),
            "unmaterialized_claim_ids": sorted(defined_ids - materialized_ids),
            "organization_supported_claim_count": sum(
                row.get("evidence_scope") == "organization" for row in claims
            ),
            "context_only_claim_count": sum(
                row.get("evidence_scope") == "context_only" for row in claims
            ),
        },
        "freshness": freshness,
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
        evidence = _enrich_evidence(con, tables, evidence)
        source_captures = {}
        if "captures" in tables:
            source_ids = {source.get("source_id") for item in evidence
                          for source in _evidence_sources(item)}
            for source_id in source_ids:
                source_captures[source_id] = _rows(
                    con, "SELECT * FROM captures WHERE source_id = ?", (source_id,)
                )
    events = []
    sources = []
    seen_events = set()
    seen_sources = set()
    for item in evidence:
        event = item.get("event")
        if event and event.get("event_id") not in seen_events:
            seen_events.add(event.get("event_id"))
            event = copy.deepcopy(event)
            event["sources"] = copy.deepcopy(_evidence_sources(item))
            events.append(event)
        for source in _evidence_sources(item):
            if source.get("source_id") in seen_sources:
                continue
            seen_sources.add(source.get("source_id"))
            sources.append(copy.deepcopy(source))
    vault_notes = _claim_vault_notes(graph, claim)
    source_notes_by_id = {}
    for source in sources:
        notes = _source_vault_notes(
            graph, source,
            captures=source_captures.get(source.get("source_id"), []),
        )
        source_notes_by_id[source.get("source_id")] = notes
        vault_notes.extend(notes)
    vault_notes = _dedupe_notes(vault_notes)
    for item in evidence:
        item_notes = []
        source_links = []
        for source in _evidence_sources(item):
            item_notes.extend(source_notes_by_id.get(source.get("source_id"), []))
            original = _outbound_url(source)
            source_links.append({
                "source_id": source.get("source_id"),
                "title": source.get("title"),
                "url": original,
                "research_path": (f"/api/research/{graph['id']}/sources/"
                                  f"{source.get('source_id')}")
                                  if source.get("source_id") else "",
            })
        item_notes = _dedupe_notes(item_notes)
        item["vault_links"] = item_notes
        item["source_links"] = source_links
        item["full_transcript"] = _transcript_note(item_notes)
        canonical = item.get("source") or {}
        # The utterance edge can point at a distribution copy.  The event's
        # canonical source is the recording/page the UI means by source.
        original = (_outbound_url(canonical) if canonical.get("source_id") else
                    _sanitize_outbound_url(item.get("source_url")))
        item["original_url"] = original
        item["timestamped_original_url"] = _timestamped_url(
            original, item.get("timestamp_seconds")
        )
    application_bridges = _claim_application_bridges(graph, claim)
    return _sanitize_public_payload({
        "graph": _public_graph(graph),
        "authority": "database",
        "claim": claim,
        "rollup": rollup,
        "organization_rollup": _scope_rollup(evidence),
        "evidence": evidence,
        "events": events,
        "sources": sources,
        "vault_notes": vault_notes,
        "application_bridges": application_bridges,
        "evidence_total": total,
        "evidence_returned": len(evidence),
        "truncated": total > len(evidence),
    })


def _claim_vault_notes(graph, claim):
    """Resolve a generated public claim projection without making it canonical."""
    root = Path(str(settings.get("vault_root") or "")).expanduser()
    if not root.is_dir():
        return []
    label = _slug(claim.get("claim_label") or claim.get("claim_id"))[:96]
    rel = Path("wiki") / f"{graph['id']}-claim-{label}.md"
    return ([{"path": rel.as_posix(),
              "title": f"Claim synthesis: {claim.get('claim_label')}",
              "context": "Generated claim page; source transcripts follow.",
              "kind": "claim_projection",
              "authority": "linked_projection"}]
            if (root / rel).is_file() else [])


def _vault_path(root, pointer):
    if not pointer:
        return ""
    try:
        path = Path(str(pointer)).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        rel = path.relative_to(root.resolve())
    except (OSError, ValueError):
        return ""
    return rel.as_posix() if path.is_file() else ""


def _source_vault_notes(graph, source, room_matches=None, captures=None):
    """Link a source directly to its summary or full raw material."""
    root = Path(str(settings.get("vault_root") or "")).expanduser()
    if not source:
        return []
    out = []
    if root.is_dir():
        pointers = [source.get("local_pointer")]
        pointers.extend((capture or {}).get("local_pointer")
                        for capture in (captures or []))
        for pointer in pointers:
            direct = _vault_path(root, pointer)
            if not direct:
                continue
            out.append({
                "path": direct,
                "title": f"Full source: {source.get('title') or Path(direct).stem}",
                "context": "Full local capture or transcript",
                "kind": "source_material",
                "action_label": _source_material_label(source),
                "source_id": source.get("source_id"),
                "authority": "linked_projection",
            })
    if room_matches is None:
        urls = {_normalize_url(source.get(key)) for key in
                ("original_url", "canonical_url") if source.get(key)}
        room_matches = _room_matches({url for url in urls if url}, graph["room"])
    for match in room_matches:
        projection = match.get("vault", {})
        path = str(projection.get("note") or "")
        kind = str(projection.get("kind") or "")
        if not path:
            item = match.get("item") or {}
            try:
                raw = fullingest.raw_path(root, item)
            except (KeyError, TypeError, ValueError):
                raw = None
            path = _vault_path(root, raw) if raw and root.is_dir() else ""
            kind = "raw_capture" if path else kind
        if not path:
            continue
        item = match.get("item") or {}
        out.append({
            "path": path,
            "title": f"Full source: {item.get('title') or source.get('title')}",
            "context": ("Full transcript or capture" if kind == "raw_capture"
                        else f"Source summary from {match.get('room', {}).get('title') or graph['room']}"),
            "kind": kind or "source_projection",
            "action_label": _source_material_label(source),
            "source_id": source.get("source_id"),
            "authority": "linked_projection",
        })
    return _dedupe_notes(out)


def _source_material_label(source):
    kind = " ".join(str(source.get(key) or "").lower() for key in
                    ("source_type", "source_family"))
    return ("Full transcript" if any(token in kind for token in
            ("audio", "conversation", "interview", "lecture", "podcast",
             "talk", "video", "webinar")) else "Full source")


def _transcript_note(notes):
    """One explicit full-source action, distinct from summaries and claims."""
    return next((copy.deepcopy(note) for note in notes
                 if note.get("kind") in {"source_material", "raw_capture"}),
                None)


def _dedupe_notes(notes):
    out = []
    seen = set()
    for note in notes:
        path = str((note or {}).get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(note)
    return out


def _timestamped_url(url, seconds):
    """Attach a time locator when the original host has a stable convention."""
    if not url or seconds is None:
        return str(url or "")
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        if host not in {"youtube.com", "www.youtube.com", "youtu.be"}:
            return str(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["t"] = f"{max(0, int(float(seconds)))}s"
        return urlunsplit((parts.scheme or "https", parts.netloc, parts.path,
                           urlencode(query), parts.fragment))
    except (TypeError, ValueError):
        return str(url)


def _evidence_classification(item):
    """Separate an explanation of a claim from a passing phrase match."""
    text = str(item.get("canonical_text") or "").strip()
    words = re.findall(r"\b[\w'’-]+\b", text)
    if item.get("mapping_method") == "manual_verified_seed":
        return "substantive", "manually audited claim evidence"
    if len(words) >= 10 and _EXPLANATION_MARKERS.search(text):
        return "substantive", "the passage explains the idea or its consequence"
    if len(words) >= 20:
        return "substantive", "the passage develops the matched idea in context"
    return "incidental", "the phrase appears without a developed explanation"


def _claim_query_tokens(value):
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1 and token not in _CLAIM_QUERY_STOPWORDS
    }


def _claim_query_score(question, claim):
    normalized = re.sub(r"[^a-z0-9]+", " ", str(question or "").lower()).strip()
    patterns = [part.strip() for part in str(claim.get("patterns") or "").split("|")
                if part.strip()]
    exact = max((len(pattern.split()) for pattern in patterns
                 if re.sub(r"[^a-z0-9]+", " ", pattern.lower()).strip() in normalized),
                default=0)
    query_tokens = _claim_query_tokens(question)
    claim_tokens = _claim_query_tokens(" ".join(str(claim.get(key) or "") for key in
                                       ("claim_label", "description", "patterns")))
    overlap = len(query_tokens & claim_tokens)
    return exact * 100 + overlap * 10 - len(claim_tokens - query_tokens) * 0.01


def _research_intent(question, graph):
    low = str(question or "").lower()
    company_terms = {
        str(graph.get("id") or "").replace("-", " ").lower(),
        str(graph.get("name") or "").lower(),
        str(graph.get("company") or "").lower(),
    }
    tokens = set(re.findall(r"[a-z0-9]+", low))
    return (any(term and term in low for term in company_terms)
            or bool(tokens & _RESEARCH_QUESTION_WORDS)
            or "where else" in low or "who else" in low)


def may_answer(question):
    """Cheap gate for ordinary chat before any graph discovery or DB work."""
    low = str(question or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", low))
    return (bool(tokens & _RESEARCH_QUESTION_WORDS)
            or "where else" in low or "who else" in low)


def _group_evidence(rows):
    grouped = {}
    for item in rows:
        speaker = str(item.get("speaker_name") or "Anthropic")
        speaker_id = str(item.get("speaker_person_id") or speaker)
        group = grouped.setdefault(speaker_id, {
            "speaker": speaker, "speaker_person_id": speaker_id, "events": [],
        })
        event = item.get("event") or {}
        event_id = str(event.get("event_id") or item.get("event_id") or "")
        event_group = next((row for row in group["events"]
                            if row["event"].get("event_id") == event_id), None)
        if event_group is None:
            event_group = {"event": copy.deepcopy(event), "evidence": []}
            group["events"].append(event_group)
        event_group["evidence"].append(item)
    return sorted(grouped.values(), key=lambda row: row["speaker"].casefold())


def _clock(seconds, locator=""):
    if seconds is None:
        return str(locator or "")
    whole = max(0, int(float(seconds)))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return (f"{hours}:{minutes:02d}:{secs:02d}" if hours
            else f"{minutes}:{secs:02d}")


def _research_markdown(graph, detail, substantive, incidental, contextual):
    claim = detail["claim"]
    rollup = _scope_rollup(substantive + incidental)
    lines = [
        f"The canonical {graph['company']} research graph maps this to "
        f"**{claim.get('claim_label')}**.",
        "",
        (f"It contains {rollup['distinct_speaker_count']} verified speakers "
         f"across {rollup['distinct_event_count']} independent events. "
         "Explanations are shown before incidental phrase matches."),
        "",
        "### Substantive definitions and explanations",
    ]
    if not substantive:
        lines.append("No developed explanations are attached yet.")
    for group in _group_evidence(substantive):
        for event_group in group["events"]:
            event = event_group["event"]
            context = " · ".join(filter(None, [
                str(event.get("event_date") or ""), str(event.get("venue") or ""),
            ]))
            lines.extend(["", f"**{group['speaker']} — {event.get('event_title') or 'Untitled event'}**"
                          + (f" ({context})" if context else "")])
            for item in event_group["evidence"]:
                lines.append(f"> {item.get('canonical_text')}")
                links = []
                original = item.get("timestamped_original_url") or item.get("original_url")
                if original:
                    links.append(f"[Original at {_clock(item.get('timestamp_seconds'), item.get('locator'))}]({original})")
                transcript = item.get("full_transcript") or {}
                if transcript.get("path"):
                    links.append(f"Full transcript [[{transcript['path']}]]")
                for link in item.get("vault_links") or []:
                    if link.get("path") and link.get("path") != transcript.get("path"):
                        links.append(f"Vault [[{link['path']}]]")
                if links:
                    lines.append(" · ".join(links))
    if incidental:
        lines.extend(["", f"### Incidental mentions ({len(incidental)})", ""])
        lines.append("These contain the phrase but do not develop the idea.")
        for item in incidental:
            event = item.get("event") or {}
            lines.append(f"- {item.get('speaker_name') or 'Anthropic'} — "
                         f"{event.get('event_title') or 'Untitled event'}: "
                         f"“{item.get('canonical_text')}”")
    if contextual:
        lines.extend(["", f"### Non-{graph['company']} context ({len(contextual)})", ""])
        lines.append("Kept separate from the organization evidence above.")
    return "\n".join(lines)


def answer_question(question, graph_id=None):
    """Answer a claim question from the canonical graph for ordinary Find chat.

    Returns ``None`` when the question does not confidently target this graph,
    allowing Find to continue through its normal vault-grounded answer path.
    """
    if graph_id is None:
        graphs = _graphs()
        low = str(question or "").casefold()
        matches = [candidate for candidate in graphs if any(
            str(candidate.get(key) or "").casefold() in low
            for key in ("id", "name", "company")
            if candidate.get(key)
        )]
        if len(matches) == 1:
            graph = matches[0]
        elif len(graphs) == 1:
            graph = graphs[0]
        else:
            return None
        if graph.get("error"):
            raise ResearchGraphError(f"{graph['id']}: {graph['error']}")
    else:
        graph = _graph(graph_id)
    if not _research_intent(question, graph):
        return None
    summary = overview(graph["id"])
    candidates = summary.get("claims") or []
    ranked = sorted(((_claim_query_score(question, claim), claim)
                     for claim in candidates), key=lambda row: row[0], reverse=True)
    if not ranked or ranked[0][0] < 10:
        return None
    detail = claim_detail(ranked[0][1]["claim_id"], graph["id"], limit=200)
    if not detail:
        return None
    organization, contextual = [], []
    for item in detail.get("evidence") or []:
        kind, reason = _evidence_classification(item)
        item["evidence_kind"] = kind
        item["classification_reason"] = reason
        (organization if item.get("evidence_scope") == "organization"
         else contextual).append(item)
    substantive = [item for item in organization
                   if item["evidence_kind"] == "substantive"]
    incidental = [item for item in organization
                  if item["evidence_kind"] == "incidental"]
    citations, hits = [], []
    seen_paths = set()
    for item in substantive + incidental:
        transcript = item.get("full_transcript") or {}
        path = transcript.get("path")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        title = transcript.get("title") or Path(path).stem
        citations.append({"ref": path, "path": path, "title": title})
        hits.append({"path": path, "title": title,
                     "heading": (item.get("event") or {}).get("event_title", ""),
                     "text": item.get("canonical_text", "")})
    return {
        "authority": "canonical_research_graph",
        "graph": _public_graph(graph),
        "claim": detail["claim"],
        "answer": _research_markdown(graph, detail, substantive, incidental,
                                     contextual),
        "substantive": _group_evidence(substantive),
        "incidental": _group_evidence(incidental),
        "context": _group_evidence(contextual),
        "citations": citations,
        "hits": hits,
    }


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


def _sanitize_outbound_url(url):
    """Return a browser-safe URL without tracking or obvious identity fields."""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        if (lowered.startswith("utm_") or lowered in _TRACKING_KEYS or
                normalized in _SENSITIVE_QUERY_KEYS or "email" in normalized or
                "@" in value):
            continue
        query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def _outbound_url(source):
    """Prefer the graph's clean canonical URL over archival/original variants."""
    for key in ("canonical_url", "original_url", "source_url", "url"):
        safe = _sanitize_outbound_url((source or {}).get(key))
        if safe:
            return safe
    return ""


def _sanitize_public_payload(value):
    """Copy an API payload while redacting unsafe URLs and local paths."""
    if isinstance(value, list):
        return [_sanitize_public_payload(item) for item in value]
    if not isinstance(value, dict):
        return (_LOCAL_PATH_TEXT.sub("[local path]", value)
                if isinstance(value, str) else value)
    out = {}
    for key, item in value.items():
        if key == "local_pointer":
            out[key] = ""
        elif (isinstance(item, str) and
              (key == "url" or key.endswith("_url"))):
            out[key] = _sanitize_outbound_url(item)
        else:
            out[key] = _sanitize_public_payload(item)
    return out


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
        canonical_sources = []
        for event in events:
            canonical_id = event.get("canonical_source_id")
            if not canonical_id and "event_sources" in tables:
                canonical_link = _row(
                    con,
                    "SELECT source_id FROM event_sources WHERE event_id = ? "
                    "AND is_canonical = 1 ORDER BY source_id LIMIT 1",
                    (event.get("event_id"),),
                )
                canonical_id = (canonical_link or {}).get("source_id")
            canonical = (_row(con, "SELECT * FROM sources WHERE source_id = ?",
                              (canonical_id,)) if canonical_id else None)
            if canonical and not any(
                    row.get("source_id") == canonical.get("source_id")
                    for row in canonical_sources):
                canonical_sources.append(canonical)
        canonical_captures = {}
        if "captures" in tables:
            for canonical in canonical_sources:
                canonical_id = canonical.get("source_id")
                canonical_captures[canonical_id] = _rows(
                    con, "SELECT * FROM captures WHERE source_id = ?",
                    (canonical_id,),
                )
        for relation in relations:
            left = str(relation.get("source_id") or "")
            right = str(relation.get("related_source_id") or "")
            if left == source_id and right != source_id:
                other_id, direction = right, "outgoing"
            elif right == source_id and left != source_id:
                other_id, direction = left, "incoming"
            else:
                other_id, direction = "", "self"
            relation["direction"] = direction
            relation["other_source_id"] = other_id
            relation["other_source"] = (_row(
                con, "SELECT * FROM sources WHERE source_id = ?", (other_id,)
            ) if other_id else None)
            if (other_id and "captures" in tables and
                    other_id not in canonical_captures):
                canonical_captures[other_id] = _rows(
                    con, "SELECT * FROM captures WHERE source_id = ?", (other_id,)
                )
    room_matches = _room_matches(urls, graph["room"])
    vault_notes = _source_vault_notes(
        graph, source, room_matches, captures=captures
    )
    canonical_source = next((row for row in canonical_sources
                             if row.get("source_id") == source_id), None)
    root_source = next((row for row in canonical_sources
                        if row.get("source_id") != source_id), None)
    if not canonical_source and not root_source:
        for relation in relations:
            relation_type = str(relation.get("relation_type") or
                                relation.get("relationship") or "").lower()
            if relation_type not in _DISTRIBUTION_RELATIONS:
                continue
            if relation.get("direction") == "outgoing":
                root_source = relation.get("other_source")
                break
            if relation.get("direction") == "incoming":
                canonical_source = source
                break
    source_role = ("distribution" if root_source else
                   "canonical" if canonical_source else "unresolved")
    local_material = _transcript_note(vault_notes)
    canonical_material = None
    if root_source:
        root_id = root_source.get("source_id")
        root_notes = _source_vault_notes(
            graph, root_source,
            captures=canonical_captures.get(root_id, []),
        )
        canonical_material = _transcript_note(root_notes)
    # A distribution record may have its own capture or Reader projection,
    # but that must never mask the canonical recording/page's full material.
    transcript = canonical_material or local_material
    transcript_source_id = ((root_source or {}).get("source_id")
                            if canonical_material else
                            source_id if local_material else "")
    for relation in relations:
        relation_type = str(relation.get("relation_type") or
                            relation.get("relationship") or "").lower()
        target_role = "related"
        if relation_type in _DISTRIBUTION_RELATIONS:
            if (relation.get("direction") == "outgoing" and root_source and
                    relation.get("other_source_id") == root_source.get("source_id")):
                target_role = "canonical"
            elif (relation.get("direction") == "incoming" and
                  source_role == "canonical"):
                target_role = "distribution"
        relation["target_role"] = target_role
        relation["navigation_label"] = {
            "canonical": "View canonical source",
            "distribution": "View distribution copy",
        }.get(target_role, "View related source")
    return _sanitize_public_payload({
        "graph": _public_graph(graph),
        # Display aliases; canonical/projections below preserve provenance.
        "source": source,
        "captures": captures,
        "events": events,
        "appearances": appearances,
        "claims": claims,
        "relations": relations,
        "vault_notes": vault_notes,
        "transcript": transcript,
        "transcript_source_id": transcript_source_id,
        "external_url": _outbound_url(source),
        "source_role": source_role,
        "root_source": root_source,
        "canonical": {
            "authority": "database",
            "source": source,
            "source_role": source_role,
            "root_source": root_source,
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
    })


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
