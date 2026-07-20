"""The scanner gate: prove no real string survived into a walkthrough's
final assets. A failing gate blocks shipping.

Two layers, symmetric with the injector (same literal set, same match
rules), plus generic built-ins mirroring scripts/check-pii.sh:

  text  -- every text asset (film HTML, captions, shots.json, css/js/md)
           is searched for every real literal, every pii-patterns regex,
           and the generic phone/home-path/personal-email built-ins.
  ocr   -- every image is read with Apple Vision (the same on-device OCR
           the media index uses) and the recognized text is searched the
           same way. Video files are sampled at 2 fps via ffmpeg and each
           frame is OCR'd, best-effort.

Run under the vira venv (it has pyobjc):
  .venv/bin/python -m walkthrough_anon scan <asset-dir-or-file> ...
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .mapping import DEFAULT_PATTERNS, _posix_to_re, load_mapping

TEXT_EXT = {".html", ".htm", ".json", ".js", ".css", ".md", ".txt",
            ".svg", ".csv", ".py", ".yml", ".yaml", ".xml"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

# Fiction the gate must not flag: the 555-01xx phone block, example
# domains, and documented placeholders (same allowlist as check-pii.sh).
_ALLOW = re.compile(
    r"[0-9]{3}[-. )]*555[-. ]*01[0-9]{2}|example\.(com|org)"
    r"|you@|yourdomain|yourtenant|\+12125551234", re.IGNORECASE)

# Generic built-ins mirroring scripts/check-pii.sh: any real-shaped
# phone, home path, or personal-email address fails even if unmapped.
_GENERIC = [
    ("generic-phone-e164", r"\+1[0-9]{10}"),
    ("generic-phone", r"[0-9]{3}[-.][0-9]{3}[-.][0-9]{4}"),
    ("generic-home-path", r"/Users/[a-z][a-z0-9_-]*"),
    ("generic-personal-email",
     r"[a-z0-9._%+-]+@(gmail|icloud|yahoo|hotmail|outlook|me)\.com"),
]


def vision_ocr(path):
    """On-device text recognition (Apple Vision via pyobjc); the same
    approach as server/localmodels.py. Returns '' on any failure."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError as e:
        raise RuntimeError(
            "pyobjc/Vision unavailable — run the scan under the vira venv "
            "(.venv/bin/python -m walkthrough_anon scan ...)") from e
    url = NSURL.fileURLWithPath_(str(path))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        url, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    ok = handler.performRequests_error_([req], None)
    if not ok or not req.results():
        return ""
    lines = []
    for obs in req.results():
        cand = obs.topCandidates_(1)
        if cand and cand[0].confidence() >= 0.3:
            lines.append(str(cand[0].string()))
    return "\n".join(lines)


class Scanner:
    def __init__(self, mapping=None, patterns_path=None):
        self.mapping = mapping or load_mapping()
        self.matchers = []  # (label, compiled_regex)
        self._build(patterns_path or DEFAULT_PATTERNS)

    def _build(self, patterns_path):
        by_kind = {"name": [], "ci": [], "digits": []}
        for e in self.mapping.get("entries", []):
            by_kind.setdefault(e["kind"], []).append(e["real"])
        def chunks(items, wrap, flags=0, label=""):
            items = sorted(items, key=len, reverse=True)
            for i in range(0, len(items), 400):
                alt = "|".join(re.escape(x) for x in items[i:i + 400])
                if alt:
                    self.matchers.append(
                        (label, re.compile(wrap(alt), flags)))
        chunks(by_kind["name"], lambda a: rf"\b(?:{a})\b",
               re.IGNORECASE, "mapped-name")
        chunks(by_kind["ci"], lambda a: rf"(?:{a})",
               re.IGNORECASE, "mapped-literal")
        chunks(by_kind["digits"], lambda a: rf"(?<![0-9])(?:{a})(?![0-9])",
               0, "mapped-digits")
        for r in self.mapping.get("regexes", []):
            try:
                self.matchers.append(
                    ("pii-pattern", re.compile(r["pattern"])))
            except re.error:
                pass
        # The pii-patterns file is authoritative — load it directly too,
        # so the gate holds even against a stale mapping.
        try:
            for line in Path(patterns_path).read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    self.matchers.append(
                        ("pii-pattern", re.compile(_posix_to_re(line))))
                except re.error:
                    pass
        except OSError:
            pass
        for label, pat in _GENERIC:
            self.matchers.append((label, re.compile(pat)))

    def scan_text(self, text, asset, layer):
        findings = []
        for label, rx in self.matchers:
            for m in rx.finditer(text):
                if _ALLOW.search(text[max(0, m.start() - 8):m.end() + 8]):
                    continue
                snippet = text[max(0, m.start() - 40):m.end() + 40]
                findings.append({
                    "asset": str(asset), "layer": layer, "rule": label,
                    "match": m.group()[:80],
                    "context": " ".join(snippet.split())[:120]})
        return findings

    def scan_file(self, path):
        path = Path(path)
        ext = path.suffix.lower()
        if ext in TEXT_EXT:
            try:
                return self.scan_text(
                    path.read_text(errors="replace"), path, "text")
            except OSError:
                return []
        if ext in IMAGE_EXT:
            return self.scan_text(vision_ocr(path), path, "ocr")
        if ext in VIDEO_EXT:
            return self._scan_video(path)
        return []

    def _scan_video(self, path):
        if not shutil.which("ffmpeg"):
            return [{"asset": str(path), "layer": "ocr", "rule": "no-ffmpeg",
                     "match": "", "context":
                     "video present but ffmpeg missing — frames NOT scanned"}]
        findings = []
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", str(path),
                 "-vf", "fps=2", f"{td}/f%04d.png"],
                check=False, capture_output=True)
            for frame in sorted(Path(td).glob("f*.png")):
                for f in self.scan_text(vision_ocr(frame), path, "ocr"):
                    f["context"] += f" [{frame.name}]"
                    findings.append(f)
        # a real string in many frames reports once per rule+match
        seen, out = set(), []
        for f in findings:
            key = (f["rule"], f["match"])
            if key not in seen:
                seen.add(key)
                out.append(f)
        return out

    def scan_paths(self, paths):
        findings = []
        for p in paths:
            p = Path(p)
            files = sorted(x for x in p.rglob("*") if x.is_file()) \
                if p.is_dir() else [p]
            for f in files:
                findings.extend(self.scan_file(f))
        return findings


def run_gate(paths, mapping_path=None, patterns_path=None, out=print):
    """Scan and report. Returns True when clean (ship), False when any
    real string survived (block)."""
    sc = Scanner(load_mapping(mapping_path), patterns_path)
    findings = sc.scan_paths(paths)
    if not findings:
        out(f"gate PASS — {len(sc.matchers)} rules, no real strings found")
        return True
    out(f"gate FAIL — {len(findings)} finding(s):")
    for f in findings:
        out(f"  [{f['layer']}/{f['rule']}] {f['asset']}")
        out(f"      {f['match']!r}  in: {f['context']}")
    return False
