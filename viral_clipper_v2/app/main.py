from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
JOBS = DATA / "jobs"

for p in (UPLOADS, JOBS):
    p.mkdir(parents=True, exist_ok=True)

OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://host.docker.internal:11434"
).rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
DEFAULT_CLIPS = int(os.getenv("CLIP_COUNT", "5"))

app = FastAPI(title="Viral Clipper", version="1.0.0")

app.mount(
    "/static",
    StaticFiles(directory=BASE / "app" / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE / "app" / "templates")

JOBS_STATE: Dict[str, Dict[str, Any]] = {}


class UrlInput(BaseModel):
    url: str
    clip_count: int = DEFAULT_CLIPS
    min_seconds: int = 25
    max_seconds: int = 60


def run(cmd: List[str], check: bool = True):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


def probe_duration(path: Path) -> float:
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    )
    return float(r.stdout.strip())


def sanitize(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:80]
    return stem or "video"


def clean_url(url: str) -> str:
    url = url.strip()

    match = re.match(r"^\[.*?\]\((https?://.*?)\)$", url)
    if match:
        url = match.group(1).strip()

    url = url.strip(" \t\r\n<>\"'")

    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        raise ValueError(
            "Please enter a valid http:// or https:// video URL."
        )

    return url


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def esc_ass(s: str) -> str:
    return (
        s.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )


def heuristic_score(text: str, start: float, end: float) -> float:
    t = text.lower()

    hooks = [
        "but","however","secret","never","always","why","how",
        "crazy","insane","best","worst","truth","mistake",
        "imagine","actually","wait","listen","problem","surprise",
        "million","money","love","hate",
    ]

    q = text.count("?") * 2 + text.count("!") * 1.5
    keyword = sum(1 for w in hooks if w in t)
    words = max(1, len(text.split()))
    dur = max(1.0, end - start)
    density = min(12, words / dur * 2.5)
    selfcontained = 4 if words >= 45 else 0

    return round(
        min(100, 45 + keyword * 3 + q + density + selfcontained),
        1,
    )


def make_windows(
    segments: List[Dict[str, Any]],
    min_s: int,
    max_s: int,
) -> List[Dict[str, Any]]:
    if not segments:
        return []

    windows = []

    for i in range(len(segments)):
        start = segments[i]["start"]
        text = []
        end = start

        for j in range(i, len(segments)):
            end = segments[j]["end"]

            if end - start > max_s:
                break

            text.append(segments[j]["text"].strip())

            if end - start >= min_s:
                joined = " ".join(text)
                windows.append(
                    {
                        "start": start,
                        "end": end,
                        "text": joined,
                        "score": heuristic_score(joined, start, end),
                    }
                )

    windows.sort(key=lambda x: x["score"], reverse=True)
    chosen = []

    for w in windows:
        if all(
            min(w["end"], c["end"]) - max(w["start"], c["start"])
            <= 0.35 * (w["end"] - w["start"])
            for c in chosen
        ):
            chosen.append(w)

        if len(chosen) >= 20:
            break

    return chosen


async def ollama_rank(
    candidates: List[Dict[str, Any]],
    count: int,
) -> List[Dict[str, Any]]:
    if not candidates:
        return []

    compact = [
        {
            "id": i,
            "start": round(c["start"], 1),
            "end": round(c["end"], 1),
            "text": c["text"][:900],
            "heuristic": c["score"],
        }
        for i, c in enumerate(candidates[:15])
    ]

    prompt = (
        "You select short-form viral video moments. "
        "Rank candidates for TikTok/Shorts/Reels. "
        "Favor strong hooks, emotion, surprise, useful insight, "
        "conflict, humor, and self-contained context. "
        f"Return ONLY JSON array with up to {count} objects: "
        '{"id":number,"score":0-100,"title":"short title",'
        '"reason":"brief reason"}. '
        f"Candidates: {json.dumps(compact, ensure_ascii=False)}"
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )

            r.raise_for_status()
            raw = r.json().get("response", "")
            obj = json.loads(raw)

            if isinstance(obj, dict):
                obj = obj.get("clips") or obj.get("results") or []

            out = []

            for item in obj:
                idx = int(item["id"])

                if 0 <= idx < len(candidates):
                    c = dict(candidates[idx])
                    c["score"] = float(item.get("score", c["score"]))
                    c["title"] = str(
                        item.get("title") or f"Viral moment {len(out) + 1}"
                    )
                    c["reason"] = str(
                        item.get("reason") or "AI-selected moment"
                    )
                    out.append(c)

            if out:
                return sorted(
                    out,
                    key=lambda x: x["score"],
                    reverse=True,
                )[:count]

    except Exception:
        pass

    out = []

    for i, c in enumerate(candidates[:count]):
        x = dict(c)
        x["title"] = f"Viral Moment {i + 1}"
        x["reason"] = "Selected using local transcript heuristics"
        out.append(x)

    return out


def transcribe(video: Path):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
    )

    segs, _ = model.transcribe(
        str(video),
        vad_filter=True,
        word_timestamps=False,
    )

    result = []

    for s in segs:
        txt = (s.text or "").strip()

        if txt:
            result.append(
                {
                    "start": float(s.start),
                    "end": float(s.end),
                    "text": txt,
                }
            )

    return result


def write_srt(
    segments,
    clip_start: float,
    clip_end: float,
    out: Path,
):
    rows = []
    n = 1

    for s in segments:
        if s["end"] < clip_start or s["start"] > clip_end:
            continue

        a = max(0, s["start"] - clip_start)
        b = min(clip_end - clip_start, s["end"] - clip_start)

        if b <= a:
            continue

        text = s["text"].strip()
        words = text.split()
        lines = []

        while words:
            lines.append(" ".join(words[:7]))
            words = words[7:]

        rows += [
            str(n),
            f"{srt_time(a)} --> {srt_time(b)}",
            "\n".join(lines),
            "",
        ]

        n += 1

    out.write_text("\n".join(rows), encoding="utf-8")


def render_clip(
    video: Path,
    jobdir: Path,
    idx: int,
    clip: Dict[str, Any],
    segments,
):
    start = float(clip["start"])
    dur = float(clip["end"] - clip["start"])

    srt = jobdir / f"clip_{idx}.srt"
    out = jobdir / f"clip_{idx}.mp4"

    write_srt(segments, start, float(clip["end"]), srt)

    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"subtitles='{esc_ass(str(srt))}':"
        "force_style='FontName=DejaVu Sans,"
        "FontSize=18,"
        "Bold=1,"
        "Alignment=2,"
        "MarginV=120,"
        "Outline=3,"
        "Shadow=1'"
    )

    cmd = [
        "ffmpeg","-y","-ss",f"{start:.3f}","-i",str(video),
        "-t",f"{dur:.3f}","-vf",vf,
        "-c:v","libx264","-preset","medium","-crf","18",
        "-c:a","aac","-b:a","192k","-movflags","+faststart",
        str(out),
    ]

    run(cmd)
    return out


async def process_job(
    job_id: str,
    video: Path,
    count: int,
    min_s: int,
    max_s: int,
):
    st = JOBS_STATE[job_id]
    jobdir = JOBS / job_id
    jobdir.mkdir(parents=True, exist_ok=True)

    try:
        st.update(
            status="transcribing",
            progress=12,
            message="Transcribing audio",
        )

        segments = await asyncio.to_thread(transcribe, video)

        (jobdir / "transcript.json").write_text(
            json.dumps(segments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if not segments:
            raise RuntimeError("No speech was detected in the video")

        st.update(
            status="analyzing",
            progress=42,
            message="Finding viral moments",
        )

        candidates = make_windows(segments, min_s, max_s)

        if not candidates:
            raise RuntimeError("Could not form suitable clip windows")

        clips = await ollama_rank(candidates, count)

        st.update(
            status="rendering",
            progress=55,
            message="Rendering vertical clips",
        )

        rendered = []

        for i, c in enumerate(clips, 1):
            out = await asyncio.to_thread(
                render_clip,
                video,
                jobdir,
                i,
                c,
                segments,
            )

            rendered.append(
                {
                    **c,
                    "file": out.name,
                    "url": f"/api/jobs/{job_id}/files/{out.name}",
                }
            )

            st["progress"] = 55 + int(40 * i / max(1, len(clips)))
            st["message"] = f"Rendered {i}/{len(clips)} clips"

        st.update(
            status="done",
            progress=100,
            message="Complete",
            clips=rendered,
        )

        (jobdir / "result.json").write_text(
            json.dumps(rendered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    except Exception as e:
        st.update(
            status="error",
            message=str(e),
            progress=100,
        )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request},
    )


@app.get("/api/health")
async def health():
    ollama = False

    try:
        async with httpx.AsyncClient(timeout=2) as c:
            ollama = (
                await c.get(f"{OLLAMA_URL}/api/tags")
            ).status_code == 200
    except Exception:
        pass

    return {
        "ok": True,
        "ollama": ollama,
        "model": OLLAMA_MODEL,
        "whisper": WHISPER_MODEL,
    }


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    clip_count: int = Form(DEFAULT_CLIPS),
    min_seconds: int = Form(25),
    max_seconds: int = Form(60),
):
    if not file.filename:
        raise HTTPException(400, "Missing filename")

    if clip_count < 1 or clip_count > 10:
        raise HTTPException(400, "clip_count must be 1-10")

    if (
        min_seconds < 10
        or max_seconds > 120
        or min_seconds >= max_seconds
    ):
        raise HTTPException(400, "Invalid clip duration")

    job_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix.lower() or ".mp4"
    video = UPLOADS / f"{job_id}_{sanitize(file.filename)}{ext}"

    with video.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        duration = probe_duration(video)
    except Exception:
        video.unlink(missing_ok=True)
        raise HTTPException(400, "Unsupported or invalid video file")

    JOBS_STATE[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 2,
        "message": "Queued",
        "filename": file.filename,
        "duration": duration,
        "clips": [],
    }

    asyncio.create_task(
        process_job(
            job_id,
            video,
            clip_count,
            min_seconds,
            max_seconds,
        )
    )

    return {"job_id": job_id}


@app.post("/api/url")
async def from_url(payload: UrlInput):
    try:
        source_url = clean_url(payload.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if payload.clip_count < 1 or payload.clip_count > 10:
        raise HTTPException(400, "clip_count must be 1-10")

    if (
        payload.min_seconds < 10
        or payload.max_seconds > 120
        or payload.min_seconds >= payload.max_seconds
    ):
        raise HTTPException(400, "Invalid clip duration")

    job_id = uuid.uuid4().hex[:12]

    JOBS_STATE[job_id] = {
        "id": job_id,
        "status": "downloading",
        "progress": 2,
        "message": "Downloading source",
        "filename": source_url,
        "clips": [],
    }

    async def dl_then_process():
        try:
            tmpl = str(UPLOADS / f"{job_id}_source.%(ext)s")

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--newline",
                "--js-runtimes",
                "node:/usr/bin/node",
                "--extractor-args",
                "youtube:player_client=default,web_embedded",
                "--retries",
                "10",
                "--fragment-retries",
                "10",
                "--socket-timeout",
                "30",
                "--concurrent-fragments",
                "1",
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                tmpl,
                source_url,
            ]

            result = await asyncio.to_thread(run, cmd, False)

            if result.returncode != 0:
                err = (
                    result.stderr
                    or result.stdout
                    or "Unknown yt-dlp error"
                ).strip()
                raise RuntimeError(err[-2500:])

            matches = list(UPLOADS.glob(f"{job_id}_source.*"))

            vids = [
                p
                for p in matches
                if p.suffix.lower()
                not in {
                    ".part",
                    ".ytdl",
                    ".json",
                    ".webp",
                    ".jpg",
                    ".jpeg",
                    ".png",
                }
            ]

            if not vids:
                raise RuntimeError(
                    "Source download failed: no video file was created"
                )

            video = max(vids, key=lambda p: p.stat().st_size)
            JOBS_STATE[job_id]["duration"] = probe_duration(video)
            JOBS_STATE[job_id]["message"] = "Source downloaded"

            await process_job(
                job_id,
                video,
                payload.clip_count,
                payload.min_seconds,
                payload.max_seconds,
            )

        except Exception as e:
            JOBS_STATE[job_id].update(
                status="error",
                progress=100,
                message=f"Download failed: {e}",
            )

    asyncio.create_task(dl_then_process())
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    st = JOBS_STATE.get(job_id)

    if not st:
        result = JOBS / job_id / "result.json"

        if result.exists():
            return {
                "id": job_id,
                "status": "done",
                "progress": 100,
                "message": "Complete",
                "clips": json.loads(
                    result.read_text(encoding="utf-8")
                ),
            }

        raise HTTPException(404, "Job not found")

    return st


@app.get("/api/jobs/{job_id}/files/{filename}")
def get_file(job_id: str, filename: str):
    safe = Path(filename).name
    p = JOBS / job_id / safe

    if not p.exists():
        raise HTTPException(404, "File not found")

    return FileResponse(
        p,
        media_type=(
            "video/mp4"
            if p.suffix == ".mp4"
            else "application/octet-stream"
        ),
        filename=safe,
    )
