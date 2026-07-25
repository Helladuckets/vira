"""Every third-party module the code imports must be declared.

The Pillow incident (2026-07-24) in one sentence: a dev machine's venv carries
transitive packages that requirements.txt never names, so `import X` can work
locally for weeks and fail on every clean install. CI installs requirements.txt
and nothing else, which is exactly the fresh-install condition — so this check
asks the question CI asks, before CI has to.

Prints nothing and exits 0 when clean; prints one line per undeclared module
and exits 1 otherwise.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN = ("server", "tests")

# import name -> distribution on PyPI, where they differ. Only needed for the
# handful that do; anything else is assumed to match its distribution name.
ALIASES = {
    "PIL": "pillow",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "claude_agent_sdk": "claude-agent-sdk",
    "pillow_heif": "pillow-heif",
}

# Imported behind a try/except ImportError AND degraded honestly at runtime.
# These are the media-index extras: deliberately not pinned (see the header of
# requirements.txt), installed on demand. Listing one here is a claim that the
# code SURVIVES its absence — if it does not, it belongs in requirements.txt.
OPTIONAL = {
    "torch", "transformers", "insightface", "mlx_whisper", "numpy",
    "pillow_heif",
    "objc", "Vision", "Quartz", "Foundation", "AppKit", "CoreFoundation",
    "pyobjc", "onnxruntime", "sentencepiece", "safetensors",
}


def declared():
    """Distribution names in requirements.txt, normalized."""
    out = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "qocha @ git+https://..." / "uvicorn[standard]" / "pkg==1.2"
        name = re.split(r"[\[@=<>;!]", line, maxsplit=1)[0].strip()
        if name:
            out.add(name.lower().replace("_", "-"))
    return out


# Stdlib on a platform this scan is not running on. msvcrt is Windows-only;
# scanning on a Mac would otherwise report it as an undeclared package.
OTHER_PLATFORM_STDLIB = {"msvcrt", "winreg", "winsound", "_winapi"}


def stdlib():
    """Standard-library top-level names.

    sys.stdlib_module_names is 3.10+. This script can be invoked by whatever
    `python3` happens to be on PATH — on macOS that is still 3.9, whose only
    fallback (builtin_module_names) lists a couple of dozen C modules and would
    report the entire standard library as undeclared. So derive it from the
    stdlib directory when the attribute is missing.
    """
    names = set(getattr(sys, "stdlib_module_names", ()))
    if names:
        return names
    import sysconfig
    names = set(sys.builtin_module_names)
    lib = sysconfig.get_paths().get("stdlib")
    if lib and pathlib.Path(lib).is_dir():
        for p in pathlib.Path(lib).iterdir():
            if p.suffix == ".py":
                names.add(p.stem)
            elif p.is_dir() and (p / "__init__.py").exists():
                names.add(p.name)
        dyn = pathlib.Path(lib) / "lib-dynload"
        if dyn.is_dir():
            for p in dyn.iterdir():
                names.add(p.name.split(".")[0])
    return names


def imported():
    """Top-level module name -> a file that imports it."""
    found = {}
    for pkg in SCAN:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        found.setdefault(a.name.split(".")[0], path)
                elif isinstance(node, ast.ImportFrom):
                    # level>0 is a relative import — always local
                    if node.level == 0 and node.module:
                        found.setdefault(node.module.split(".")[0], path)
    return found


def main():
    local = {p.name for p in ROOT.iterdir()} | {"server", "tests", "walkthrough_anon"}
    std, decl = stdlib(), declared()
    missing = []
    for mod, path in sorted(imported().items()):
        if mod in std or mod in local or mod in OPTIONAL:
            continue
        if mod in OTHER_PLATFORM_STDLIB:
            continue
        dist = ALIASES.get(mod, mod).lower().replace("_", "-")
        if dist not in decl:
            missing.append(f"{mod} (-> {dist}) imported by {path.relative_to(ROOT)}")
    if missing:
        print("\n".join(missing))
        return 1
    print(f"{len(imported())} top-level imports, all declared or stdlib/local/optional")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
