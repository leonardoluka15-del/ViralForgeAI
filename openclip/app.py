import os, json, shutil, subprocess, uuid
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
import imageio_ffmpeg
import requests

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "media"
SRC = MEDIA / "source"
CLIPS = MEDIA / "clips"
for p in (SRC, CLIPS):
    p.mkdir(parents=True, exist_ok=True)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

app = FastAPI(title="OpenClip Render")
app.mount("/media", StaticFiles(directory=str(MEDIA)), name="media")

_whisper = None
def whisper():
    global _whisper
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper

def transcribe(path: str):
    segs, _ = whisper().transcribe(path, beam_size=1, vad_filter=True)
    return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()} for s in segs]

def make_candidates(segs, target=45, max_len=70):
    out = []
    i = 0
    while i < len(segs):
        start = segs[i]["start"]
        texts, end, j = [], start, i
        while j < len(segs):
            if segs[j]["end"] - start > max_len:
                break
            texts.append(segs[j]["text"])
            end = segs[j]["end"]
            j += 1
            if end - start >= target:
                break
        if end - start >= 15:
            out.append({"start": start, "end": end, "text": " ".join(texts)})
        i = max(i + 1, j)
    return out[:8]

def heuristic_score(text: str):
    words = text.split()
    base = min(88, 45 + len(words) * 0.18)
    punctuation = text.count("!") * 2 + text.count("?") * 2
    return min(95, round(base + punctuation, 1))

def llama_score(text: str):
    if not OLLAMA_BASE_URL:
        return {
            "score": heuristic_score(text),
            "hook": text[:120].strip(),
            "title": (text[:70].strip() or "Clip"),
            "hashtags": "#shorts #viral #clip",
            "rationale": "Heuristic fallback used because OLLAMA_BASE_URL is not configured."
        }
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": "You select viral short-form video moments. Return only JSON with keys score, hook, title, hashtags, rationale. Score 0-100. Do not invent facts."},
            {"role": "user", "content": "Transcript:\n" + text}
        ]
    }
    r = requests.post(OLLAMA_BASE_URL + "/api/chat", json=payload, timeout=120)
    r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    data["score"] = float(data.get("score", 0))
    if isinstance(data.get("hashtags"), list):
        data["hashtags"] = " ".join(data["hashtags"])
    return data

def render_vertical(src: str, start: float, end: float, out_path: str):
    duration = max(0.1, end - start)
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    cmd = [
        FFMPEG, "-y", "-ss", str(start), "-i", src, "-t", str(duration),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-3000:])

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>OpenClip AI</title>
<style>
body{font-family:Arial,sans-serif;background:#0b0d10;color:#f5f7fa;margin:0}
.wrap{max-width:1100px;margin:0 auto;padding:32px}
.card{background:#161a20;border:1px solid #2b313a;border-radius:16px;padding:20px;margin:18px 0}
h1{font-size:38px;margin:0 0 8px} .muted{color:#aab4c3}
button{background:#fff;color:#111;border:0;border-radius:10px;padding:12px 18px;font-weight:700;cursor:pointer}
input{background:#0f1217;color:#fff;border:1px solid #39424e;border-radius:9px;padding:11px;width:100%;box-sizing:border-box}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.score{font-size:28px;font-weight:800}
a{color:#8bc5ff}
video{width:100%;border-radius:12px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body><div class="wrap">
<h1>✂️ OpenClip AI</h1>
<p class="muted">Upload → transcribe → find viral moments → render 9:16 → download</p>
<div class="card">
<form action="/analyze" method="post" enctype="multipart/form-data">
<label>Upload a video</label><br><br>
<input type="file" name="file" accept="video/*" required><br><br>
<button type="submit">Analyze video</button>
</form>
</div>
<p class="muted">AI model: {{MODEL_STATUS}} · Whisper: {{WHISPER}}</p>
</div></body></html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    status = OLLAMA_MODEL if OLLAMA_BASE_URL else "fallback scoring (Llama endpoint not connected yet)"
    return HTML.replace("{{MODEL_STATUS}}", status).replace("{{WHISPER}}", WHISPER_MODEL)

@app.get("/health")
def health():
    return {"ok": True, "model": OLLAMA_MODEL, "ollama_connected": bool(OLLAMA_BASE_URL)}

@app.post("/analyze", response_class=HTMLResponse)
async def analyze(file: UploadFile = File(...)):
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    ident = uuid.uuid4().hex[:10]
    src_path = SRC / f"{ident}{ext}"
    with src_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        segs = transcribe(str(src_path))
        candidates = make_candidates(segs)
        ranked = []
        for c in candidates:
            try:
                ai = llama_score(c["text"])
            except Exception as e:
                ai = {
                    "score": heuristic_score(c["text"]),
                    "hook": c["text"][:120],
                    "title": c["text"][:70],
                    "hashtags": "#shorts #viral #clip",
                    "rationale": "Fallback scoring used: " + str(e)[:120]
                }
            ranked.append({**c, **ai})
        ranked.sort(key=lambda x: x.get("score",0), reverse=True)
    except Exception as e:
        raise HTTPException(500, f"Analysis failed: {e}")

    cards = []
    for idx, c in enumerate(ranked[:6], start=1):
        payload = json.dumps({"src": str(src_path), "start": c["start"], "end": c["end"], "ident": ident, "idx": idx})
        cards.append(f"""
        <div class="card">
          <div class="score">Score {c.get('score',0):.0f}/100</div>
          <h3>{c.get('title','Candidate clip')}</h3>
          <p>{c.get('hook','')}</p>
          <p class="muted">{c['start']:.1f}s → {c['end']:.1f}s · {c.get('hashtags','')}</p>
          <details><summary>Transcript</summary><p>{c['text']}</p></details><br>
          <form action="/render" method="post">
            <input type="hidden" name="payload" value='{payload.replace("'", "&#39;")}'>
            <button type="submit">Render 9:16 clip</button>
          </form>
        </div>
        """)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>OpenClip Results</title>
    <style>body{{font-family:Arial;background:#0b0d10;color:#f5f7fa}}.wrap{{max-width:1000px;margin:auto;padding:30px}}
    .card{{background:#161a20;border:1px solid #2b313a;border-radius:16px;padding:20px;margin:18px 0}}
    .score{{font-size:28px;font-weight:800}}.muted{{color:#aab4c3}}button{{padding:12px 18px;border:0;border-radius:10px;font-weight:700}}</style>
    </head><body><div class="wrap"><a href="/" style="color:#8bc5ff">← New video</a><h1>Candidate clips</h1>{''.join(cards)}</div></body></html>"""

@app.post("/render", response_class=HTMLResponse)
def render(payload: str = Form(...)):
    data = json.loads(payload)
    name = f"{data['ident']}-clip-{data['idx']}.mp4"
    out = CLIPS / name
    render_vertical(data["src"], float(data["start"]), float(data["end"]), str(out))
    url = f"/media/clips/{name}"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>OpenClip Export</title>
    <style>body{{font-family:Arial;background:#0b0d10;color:#fff}}.wrap{{max-width:700px;margin:auto;padding:30px}}
    .card{{background:#161a20;border-radius:16px;padding:20px}}video{{width:100%;border-radius:12px}}a{{color:#8bc5ff}}</style>
    </head><body><div class="wrap"><div class="card"><h1>Clip ready</h1>
    <video controls src="{url}"></video><p><a href="/download/{name}">Download MP4</a></p>
    <p><a href="/">Process another video</a></p></div></div></body></html>"""

@app.get("/download/{name}")
def download(name: str):
    p = CLIPS / Path(name).name
    if not p.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(p), media_type="video/mp4", filename=p.name)
