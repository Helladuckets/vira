#!/usr/bin/env python3
"""One-shot: seed data/subscriptions.json from a prior merchant catalog
(a JSON map of lowercase-alias -> {display_name, url, description, category,
expected_cadence}), so no existing curation is lost in a migration.

  .venv/bin/python scripts/seed-subscriptions.py <path-to-catalog.json> [--force]

Mapping: catalog key -> alias (plus the lowercased display name when it
differs — a bank counterparty like "Voyage Ai" must match a catalog key of
"voyage"); expected_cadence -> cadence_override for real cadences,
"one-off" -> "one-time", "usage" -> null (usage billing has no cadence to
override; observation decides). Refuses to overwrite an existing registry
without --force — the registry is curated, non-regenerable state.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import subscriptions  # noqa: E402

CADENCE_MAP = {"monthly": "monthly", "quarterly": "quarterly",
               "semi-annual": "semi-annual", "annual": "annual",
               "one-off": "one-time"}


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    if not args:
        sys.exit("usage: seed-subscriptions.py <path-to-catalog.json> [--force]")
    catalog_path = Path(args[0]).expanduser()

    if subscriptions.REGISTRY.exists() and not force:
        sys.exit(f"{subscriptions.REGISTRY} already exists — pass --force to reseed")

    catalog = json.loads(catalog_path.read_text())
    merchants = []
    for key, entry in catalog.items():
        display = entry.get("display_name") or key.title()
        aliases = [key.lower()]
        if display.lower() not in aliases:
            aliases.append(display.lower())
        merchants.append({
            "id": subscriptions.slugify(key),
            "display_name": display,
            "aliases": aliases,
            "url": entry.get("url", ""),
            "category": entry.get("category", "Unknown"),
            "cadence_override": CADENCE_MAP.get(entry.get("expected_cadence")),
            "status": "active",
            "note": entry.get("description", ""),
            "receipt_senders": [],
        })

    subscriptions.save_registry({"merchants": merchants})
    print(f"seeded {len(merchants)} merchants -> {subscriptions.REGISTRY}")


if __name__ == "__main__":
    main()
