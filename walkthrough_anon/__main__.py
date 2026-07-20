"""CLI for the walkthrough anonymization layer.

  python -m walkthrough_anon build [--out PATH] [--crm PATH]
      Build (or rebuild) the deterministic mapping from the CRM,
      pii-patterns, and owner config. Prints a summary, never a name.

  python -m walkthrough_anon scan PATH [PATH ...] [--mapping PATH]
      The gate: scan final walkthrough assets (text + OCR). Exit 0 on
      pass, 1 on fail — wire it into the ship step. Needs the vira venv
      for Apple Vision OCR.

  python -m walkthrough_anon payload [--mapping PATH]
      Print the injector payload JSON (for non-python capture harnesses).
"""
import argparse
import json
import sys

from . import Anonymizer
from .mapping import DEFAULT_MAPPING, build_mapping, save_mapping
from .scan import run_gate


def main(argv=None):
    ap = argparse.ArgumentParser(prog="walkthrough_anon")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--out", default=None)
    b.add_argument("--crm", default=None)
    b.add_argument("--patterns", default=None)
    b.add_argument("--config", default=None)

    s = sub.add_parser("scan")
    s.add_argument("paths", nargs="+")
    s.add_argument("--mapping", default=None)
    s.add_argument("--patterns", default=None)

    p = sub.add_parser("payload")
    p.add_argument("--mapping", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "build":
        m = build_mapping(args.crm, args.patterns, args.config)
        path = save_mapping(m, args.out)
        kinds = {}
        for e in m["entries"]:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        print(f"mapping written: {path}")
        print(f"  entries: {len(m['entries'])} "
              f"({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})")
        print(f"  regexes: {len(m['regexes'])}  avatars: {len(m['avatars'])}")
        return 0

    if args.cmd == "scan":
        ok = run_gate(args.paths, args.mapping, args.patterns)
        return 0 if ok else 1

    if args.cmd == "payload":
        print(json.dumps(Anonymizer(args.mapping).payload()))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
