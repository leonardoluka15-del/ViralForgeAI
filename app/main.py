from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid, shutil, threading
from .pipeline import process_video

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app' / 'static'
UPLOADS = ROOT / 'data' / 'uploads'
OUTPUTS = ROOT / 'data' / 'outputs'
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title='ViralForge AI', version='1.0.0')
app.mount('/static', StaticFiles(directory=STATIC), name='static')
app.mount('/media', StaticFiles(directory=OUTPUTS), name='media')

jobs = {}
lock = threading.Lock()

@app.get('/')
def home():
    return FileResponse(STATIC / 'index.html')

@app.get('/health')
def health():
    return {'ok': True, 'service': 'ViralForge AI'}

@app.post('/api/jobs')
async def create_job(background_tasks: BackgroundTasks, video: UploadFile = File(...), clip_length: int = Form(35)):
    ext = Path(video.filename or 'video.mp4').suffix.lower()
    if ext not in {'.mp4','.mov','.mkv','.webm','.avi','.m4v'}:
        raise HTTPException(400, 'Unsupported video type.')
    if clip_length not in {15,30,35,45,60}:
        clip_length = 35

    jid = uuid.uuid4().hex[:12]
    src = UPLOADS / f'{jid}{ext}'
    with src.open('wb') as f:
        shutil.copyfileobj(video.file, f)

    with lock:
        jobs[jid] = {
            'id': jid, 'status': 'queued', 'progress': 3,
            'message': 'Upload complete. Preparing AI analysis…',
            'clips': [], 'filename': video.filename, 'clip_length': clip_length
        }
    background_tasks.add_task(_run_job, jid, src, clip_length)
    return {'job_id': jid}

def _run_job(jid: str, src: Path, clip_length: int):
    def update(progress, message):
        with lock:
            jobs[jid]['status'] = 'processing'
            jobs[jid]['progress'] = progress
            jobs[jid]['message'] = message
    try:
        out_dir = OUTPUTS / jid
        result = process_video(src, out_dir, update, clip_length=clip_length)
        with lock:
            jobs[jid].update({
                'status':'done','progress':100,
                'message':'Your AI clips are ready.','clips':result
            })
    except Exception as e:
        with lock:
            jobs[jid].update({'status':'error','progress':100,'message':str(e)})

@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    with lock:
        if job_id not in jobs:
            raise HTTPException(404, 'Job not found')
        return jobs[job_id]
