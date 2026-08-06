#!/usr/bin/env python3
"""Stage only canonical ``ingest_full`` roots from an audited TC-IL delta.

Inspection is the default.  Vault and network writes require ``--apply``.
The manifest is projected through a fixed public Reader schema; arbitrary
manifest fields are never copied into TC-IL.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import fullingest, readingroom  # noqa: E402


class SelectionError(ValueError):
    pass


def _priority(value):
    return {"high_signal": "P1", "P1": "P1", "P2": "P2"}.get(
        str(value or ""), "P3")


def _mode(url):
    if fullingest.classify(url) == "youtube":
        return "watch"
    host = urlparse(url).netloc.lower()
    return "listen" if host in {"omny.fm", "iheart.com", "www.iheart.com"} else "read"


def selection(manifest, expected_count):
    """Validate the action boundary and return public, Reader-clean items."""
    if not isinstance(manifest, dict):
        raise SelectionError("manifest must be an object")
    selected = manifest.get("ingest_full")
    pointers = manifest.get("pointer_only_distribution") or []
    bibliography = manifest.get("bibliography_only") or []
    if not isinstance(selected, list):
        raise SelectionError("manifest.ingest_full must be a list")
    if len(selected) != expected_count:
        raise SelectionError(
            f"expected {expected_count} ingest_full roots, found {len(selected)}")
    declared = (((manifest.get("counts") or {}).get("by_action") or {})
                .get("ingest_full"))
    if declared != len(selected):
        raise SelectionError(
            f"manifest declares {declared!r} ingest_full roots, found {len(selected)}")

    def ids(rows, label, fallback=""):
        if not isinstance(rows, list):
            raise SelectionError(f"manifest.{label} must be a list")
        out = [str(row.get("source_id") or row.get(fallback) or "") for row in rows
               if isinstance(row, dict)]
        if len(out) != len(rows) or any(not value for value in out):
            suffix = f" or {fallback}" if fallback else ""
            raise SelectionError(f"every {label} row needs source_id{suffix}")
        return out

    selected_ids = ids(selected, "ingest_full")
    excluded_ids = set(ids(pointers, "pointer_only_distribution")) \
        | set(ids(bibliography, "bibliography_only", "reader_item_id"))
    if len(set(selected_ids)) != len(selected_ids):
        raise SelectionError("ingest_full contains duplicate source_id values")
    overlap = sorted(set(selected_ids) & excluded_ids)
    if overlap:
        raise SelectionError(
            "excluded source IDs also appear in ingest_full: " + ", ".join(overlap))
    selected_urls = {str(row.get("url") or "").strip().lower()
                     for row in selected if isinstance(row, dict)}
    excluded_urls = {str(row.get("url") or "").strip().lower()
                     for row in pointers + bibliography if isinstance(row, dict)}
    url_overlap = sorted((selected_urls & excluded_urls) - {""})
    if url_overlap:
        raise SelectionError(
            "excluded URLs also appear in ingest_full: " + ", ".join(url_overlap))

    projected = []
    for index, row in enumerate(selected):
        if not isinstance(row, dict):
            raise SelectionError(f"ingest_full[{index}] must be an object")
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise SelectionError(f"ingest_full[{index}] has invalid URL")
        source_id = str(row.get("source_id") or "").strip()
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            raise SelectionError(f"ingest_full[{index}] needs event_id")
        host = urlparse(url).netloc.lower().removeprefix("www.")
        projected.append({
            "title": str(row.get("title") or "").strip(),
            "url": url,
            "date": str(row.get("publication_date") or "").strip(),
            "mode": _mode(url),
            "status": "MISSING",
            "prio": _priority(row.get("priority")),
            "people": [],
            "type": "canonical source",
            "venue": host,
            "note": (f"Validated canonical root for {int(row.get('claim_count') or 0)} "
                     "claim-linked finding(s)."),
            "why": "Canonical public evidence selected by the audited research graph.",
            "vault": "",
            "research_graph": "anthropic",
            "research_source_id": source_id,
            "research_event_id": event_id,
            "pay": False,
        })
    try:
        clean = readingroom.clean_items(projected)
    except readingroom.BuildError as exc:
        raise SelectionError(str(exc)) from exc
    if len(clean) != expected_count:
        raise SelectionError(
            f"Reader normalization collapsed {expected_count} roots to {len(clean)}; "
            "resolve duplicate URLs before staging")
    return clean


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-count", required=True, type=int)
    parser.add_argument("--room-slug", default="anthropic-universe")
    parser.add_argument("--vault-root", type=Path)
    parser.add_argument("--reader-rooms-dir", type=Path,
                        help="room-store directory to receive the same URL-stable items")
    parser.add_argument("--merge-reader", action="store_true",
                        help="merge selected roots into the named Reader room")
    parser.add_argument("--apply", action="store_true",
                        help="perform network fetches and immutable vault writes")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = selection(manifest, args.expected_count)
    if not args.apply:
        result = {
            "mode": "dry-run",
            "room": args.room_slug,
            "selected": len(items),
            "source_ids": [it["research_source_id"] for it in items],
            "excluded": {
                "pointer_only_distribution": len(
                    manifest.get("pointer_only_distribution") or []),
                "bibliography_only": len(manifest.get("bibliography_only") or []),
            },
        }
    else:
        if args.vault_root is None:
            parser.error("--vault-root is required with --apply")
        if args.merge_reader and args.reader_rooms_dir is None:
            parser.error("--reader-rooms-dir is required with --merge-reader")
        result = fullingest.stage_items(
            items, args.room_slug, root=args.vault_root)
        if args.merge_reader:
            readingroom.ROOMS_DIR = args.reader_rooms_dir
            result["reader"] = readingroom.merge_items(args.room_slug, items)
        result["mode"] = "apply"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
