"""Lazy singletons for every local model the media index uses. All
inference is on-device: SigLIP2 (MPS) for scene embeddings, Apple Vision
for OCR, InsightFace for face identity, Ollama (resident daemon) for text
embeddings + captions, mlx-whisper for transcripts.

Each loader initializes once per process behind a lock; callers in the
indexer treat failures as skip-and-resume, so a missing daemon or a
still-downloading model never wedges a backfill stage.
"""
import base64
import io
import json
import threading
import urllib.request
from pathlib import Path

import numpy as np

from . import settings

SIGLIP_MODEL = "google/siglip2-so400m-patch14-384"
OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
CAPTION_MODEL = "gemma3:4b"
WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"

_lock = threading.Lock()          # guards one-time model loading
# Serializes SigLIP forward passes. torch-MPS kernel dispatch is NOT
# thread-safe — two threads in a Metal shader-library lookup at once corrupt
# its shared cache (SIGBUS). The query path (search.py, on an anyio worker
# thread) and the background Indexer's scene stage both embed on MPS, so every
# forward pass must hold this lock.
_infer_lock = threading.Lock()
_siglip = {}
_insight = {}


def _pil():
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image


# ---------- SigLIP (scene space) ----------

def _load_siglip():
    with _lock:
        if "model" not in _siglip:
            import torch
            from transformers import AutoModel, AutoProcessor
            dev = "mps" if torch.backends.mps.is_available() else "cpu"
            _siglip["model"] = AutoModel.from_pretrained(
                SIGLIP_MODEL, dtype=torch.float32).to(dev).eval()
            _siglip["proc"] = AutoProcessor.from_pretrained(SIGLIP_MODEL)
            _siglip["dev"] = dev
    return _siglip


def siglip_embed_images(paths):
    """List of unit vectors (or None per unreadable file)."""
    import torch
    s = _load_siglip()
    Image = _pil()
    imgs, ok = [], []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((1024, 1024))
            imgs.append(im)
            ok.append(True)
        except Exception:  # noqa: BLE001 — corrupt/unsupported file
            ok.append(False)
    out = [None] * len(paths)
    if imgs:
        with _infer_lock:                     # serialize MPS dispatch (see _infer_lock)
            with torch.no_grad():
                inp = s["proc"](images=imgs, return_tensors="pt").to(s["dev"])
                v = s["model"].get_image_features(**inp)
                if hasattr(v, "pooler_output"):
                    v = v.pooler_output
                v = v / v.norm(dim=-1, keepdim=True)
            v = v.float().cpu().numpy()
        j = 0
        for i, good in enumerate(ok):
            if good:
                out[i] = v[j]
                j += 1
    return out


def siglip_embed_text(q):
    import torch
    s = _load_siglip()
    with _infer_lock:                         # serialize with the indexer's scene stage
        with torch.no_grad():
            inp = s["proc"](text=[q.lower()], padding="max_length",
                            max_length=64, truncation=True,
                            return_tensors="pt").to(s["dev"])
            v = s["model"].get_text_features(**inp)
            if hasattr(v, "pooler_output"):
                v = v.pooler_output
            v = v / v.norm(dim=-1, keepdim=True)
        return v.float().cpu().numpy()[0]


# ---------- Apple Vision OCR ----------

def ocr_available():
    """Whether this machine can OCR at all. Apple Vision is the only
    backend today, so non-Macs answer False — and the OCR stage must SKIP
    rather than run, or every photo gets permanently stamped "OCR ran,
    nothing found" on a platform where it never ran."""
    if not settings.IS_MAC:
        return False
    try:
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def vision_ocr(path):
    """On-device text recognition; reads HEIC natively. Returns ''
    when nothing is found or the file can't be read."""
    import Vision
    from Foundation import NSURL
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


# ---------- InsightFace (identity space) ----------

def _load_insight():
    with _lock:
        if "app" not in _insight:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name="buffalo_l",
                               providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            _insight["app"] = app
    return _insight["app"]


def face_analyze(path):
    """[{bbox, det_score, v}] — v is the 512-d ArcFace identity vector."""
    Image = _pil()
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((1600, 1600))
        arr = np.asarray(im)[:, :, ::-1]      # RGB -> BGR
    except Exception:  # noqa: BLE001
        return []
    faces = _load_insight().get(arr)
    return [{"bbox": list(map(float, f.bbox)),
             "det_score": float(f.det_score),
             "v": f.normed_embedding.astype("float32")}
            for f in faces if f.det_score >= 0.5]


# ---------- Ollama (text embeddings + captions) ----------

def _ollama(endpoint, payload, timeout=180):
    req = urllib.request.Request(
        OLLAMA + endpoint, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 — daemon down/model missing
        return None


def ollama_embed(texts):
    """List of unit vectors, or None when Ollama is unreachable."""
    r = _ollama("/api/embed", {"model": EMBED_MODEL, "input": texts})
    if not r or "embeddings" not in r:
        return None
    out = []
    for e in r["embeddings"]:
        v = np.array(e, dtype="float32")
        out.append(v / (np.linalg.norm(v) + 1e-9))
    return out


def ollama_caption(path, prompt):
    """One dense caption via the local VLM, or None when unreachable."""
    Image = _pil()
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((768, 768))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    except Exception:  # noqa: BLE001
        return " "
    r = _ollama("/api/chat", {
        "model": CAPTION_MODEL, "stream": False,
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(buf.getvalue()).decode()]}],
    })
    if not r:
        return None
    return (r.get("message") or {}).get("content", " ")


# ---------- mlx-whisper (transcripts) ----------

def whisper_transcribe(path):
    try:
        import mlx_whisper
        r = mlx_whisper.transcribe(str(path), path_or_hf_repo=WHISPER_REPO)
        return r.get("text", "")
    except Exception:  # noqa: BLE001 — undecodable media is a blank, not a crash
        return ""
