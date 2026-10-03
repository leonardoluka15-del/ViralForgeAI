from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid, shutil, threading, urllib.parse, os, secrets
import yt_dlp
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request as GoogleRequest
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

YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.upload','https://www.googleapis.com/auth/youtube.readonly']
YOUTUBE_REDIRECT = 'https://viralforge-ai-jwuo.onrender.com/youtube/callback'
oauth_states = set()

def youtube_client_config():
    cid = os.getenv('YOUTUBE_CLIENT_ID','').strip()
    secret = os.getenv('YOUTUBE_CLIENT_SECRET','').strip()
    if not cid or not secret:
        raise HTTPException(503, 'YouTube OAuth is not configured yet. Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in Render Environment.')
    return {
        'web': {
            'client_id': cid,
            'client_secret': secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [YOUTUBE_REDIRECT],
        }
    }

@app.get('/youtube/connect')
def youtube_connect():
    flow = Flow.from_client_config(youtube_client_config(), scopes=YOUTUBE_SCOPES, redirect_uri=YOUTUBE_REDIRECT)
    state = secrets.token_urlsafe(24)
    oauth_states.add(state)
    url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
        state=state
    )
    return RedirectResponse(url)

@app.get('/youtube/callback', response_class=HTMLResponse)
def youtube_callback(request: Request, state: str = '', code: str = ''):
    if not state or state not in oauth_states:
        raise HTTPException(400, 'Invalid or expired OAuth state. Start again from /youtube/connect.')
    oauth_states.discard(state)
    flow = Flow.from_client_config(
        youtube_client_config(),
        scopes=YOUTUBE_SCOPES,
        state=state,
        redirect_uri=YOUTUBE_REDIRECT
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    refresh = creds.refresh_token or ''
    if not refresh:
        return HTMLResponse('<h2>No refresh token was returned.</h2><p>Revoke the app in your Google account and connect again with consent.</p>', status_code=400)
    safe = refresh.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    return HTMLResponse(f"""<!doctype html><html><body style="font-family:system-ui;background:#0b0d10;color:white;padding:40px;max-width:900px;margin:auto">
    <h1 style="color:#c9ff3d">YouTube connected</h1>
    <p>Copy the token below and paste it directly into Render as <b>YOUTUBE_REFRESH_TOKEN</b>.</p>
    <p><b>Do not send this token in ChatGPT, email, or messages.</b></p>
    <textarea readonly style="width:100%;height:120px;background:#15181e;color:white;border:1px solid #444;border-radius:10px;padding:12px">{safe}</textarea>
    <p>After saving it in Render, redeploy ViralForge. Then the server can upload to your YouTube channel without you being present.</p>
    </body></html>""")

def youtube_credentials():
    cid = os.getenv('YOUTUBE_CLIENT_ID','').strip()
    secret = os.getenv('YOUTUBE_CLIENT_SECRET','').strip()
    refresh = os.getenv('YOUTUBE_REFRESH_TOKEN','').strip()
    if not cid or not secret or not refresh:
        raise HTTPException(503, 'YouTube credentials are incomplete in Render Environment.')
    creds = Credentials(
        token=None,
        refresh_token=refresh,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=cid,
        client_secret=secret,
        scopes=YOUTUBE_SCOPES,
    )
    creds.refresh(GoogleRequest())
    return creds

@app.get('/api/youtube/status')
def youtube_status():
    cid = bool(os.getenv('YOUTUBE_CLIENT_ID','').strip())
    secret = bool(os.getenv('YOUTUBE_CLIENT_SECRET','').strip())
    refresh = bool(os.getenv('YOUTUBE_REFRESH_TOKEN','').strip())
    base = {'configured': cid and secret and refresh, 'client_id': cid, 'client_secret': secret, 'refresh_token': refresh}
    if not base['configured']:
        return base
    try:
        yt = build('youtube','v3',credentials=youtube_credentials(),cache_discovery=False)
        resp = yt.channels().list(part='snippet,statistics', mine=True).execute()
        items = resp.get('items',[])
        if items:
            ch = items[0]
            base.update({
                'authorized': True,
                'channel_id': ch.get('id'),
                'channel_title': ch.get('snippet',{}).get('title'),
                'subscribers': ch.get('statistics',{}).get('subscriberCount'),
                'videos': ch.get('statistics',{}).get('videoCount'),
            })
        else:
            base.update({'authorized': False, 'error': 'No YouTube channel found for this Google account.'})
    except Exception as e:
        base.update({'authorized': False, 'error': str(e)[:240]})
    return base

@app.post('/api/youtube/upload')
def youtube_upload(
    job_id: str = Form(...),
    rank: int = Form(1),
    title: str = Form(''),
    description: str = Form(''),
    privacy: str = Form('private'),
    publish_at: str = Form('')
):
    if privacy not in {'private','unlisted','public'}:
        privacy = 'private'
    with lock:
        job = jobs.get(job_id)
        if not job or job.get('status') != 'done':
            raise HTTPException(404, 'Completed job not found.')
        clips = job.get('clips',[])
        clip = next((c for c in clips if int(c.get('rank',0)) == int(rank)), None)
    if not clip:
        raise HTTPException(404, 'Clip not found.')

    rel = clip.get('url','').replace('/media/','',1)
    video_path = OUTPUTS / rel
    if not video_path.exists():
        raise HTTPException(404, 'Rendered clip file is no longer available on this server.')

    final_title = (title.strip() or clip.get('title') or f'ViralForge Short #{rank}')[:100]
    final_description = description.strip() or '#Shorts'
    if '#shorts' not in final_description.lower():
        final_description = final_description.rstrip() + '\n\n#Shorts'

    status = {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False}
    if publish_at.strip():
        status['privacyStatus'] = 'private'
        status['publishAt'] = publish_at.strip()

    body = {
        'snippet': {
            'title': final_title,
            'description': final_description,
            'categoryId': '22',
        },
        'status': status,
    }

    try:
        yt = build('youtube','v3',credentials=youtube_credentials(),cache_discovery=False)
        media = MediaFileUpload(str(video_path), mimetype='video/mp4', resumable=True, chunksize=8*1024*1024)
        req = yt.videos().insert(part='snippet,status', body=body, media_body=media)
        response = None
        while response is None:
            _, response = req.next_chunk()
        return {
            'ok': True,
            'video_id': response.get('id'),
            'title': final_title,
            'privacy': status.get('privacyStatus'),
            'publish_at': status.get('publishAt'),
            'youtube_url': f"https://www.youtube.com/watch?v={response.get('id')}"
        }
    except Exception as e:
        raise HTTPException(500, f'YouTube upload failed: {str(e)[:400]}')


@app.post('/api/jobs')
async def create_job(background_tasks: BackgroundTasks, video: UploadFile | None = File(None), video_url: str = Form(''), clip_length: int = Form(35)):
    if clip_length not in {15,30,35,45,60}:
        clip_length = 35

    jid = uuid.uuid4().hex[:12]
    src = None
    display_name = None

    if video and video.filename:
        ext = Path(video.filename or 'video.mp4').suffix.lower()
        if ext not in {'.mp4','.mov','.mkv','.webm','.avi','.m4v'}:
            raise HTTPException(400, 'Unsupported video type.')
        src = UPLOADS / f'{jid}{ext}'
        with src.open('wb') as f:
            shutil.copyfileobj(video.file, f)
        display_name = video.filename
    elif video_url.strip():
        parsed = urllib.parse.urlparse(video_url.strip())
        host = (parsed.hostname or '').lower()
        allowed = (
            'youtube.com','www.youtube.com','youtu.be',
            'tiktok.com','www.tiktok.com','vm.tiktok.com',
            'instagram.com','www.instagram.com',
            'x.com','www.x.com','twitter.com','www.twitter.com',
            'vimeo.com','www.vimeo.com'
        )
        if host not in allowed:
            raise HTTPException(400, 'Unsupported video link. Use YouTube, TikTok, Instagram, X/Twitter, or Vimeo.')
        src = UPLOADS / f'{jid}.mp4'
        try:
            ydl_opts = {
                'format': 'best[ext=mp4][height<=720]/best[height<=720]/best',
                'merge_output_format': 'mp4',
                'outtmpl': str(src),
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
            }
            if host in {'youtube.com','www.youtube.com','youtu.be'}:
                ydl_opts['extractor_args'] = {
                    'youtube': {'player_client': ['android']}
                }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url.strip(), download=True)
                display_name = info.get('title') or video_url.strip()
        except Exception as e:
            msg = str(e)[:220]
            if 'confirm you’re not a bot' in msg.lower() or 'confirm you\'re not a bot' in msg.lower():
                msg = 'YouTube blocked this server request. Try again once; if it persists, upload the video file instead.'
            raise HTTPException(400, f'Could not fetch that video link: {msg}')
    else:
        raise HTTPException(400, 'Choose a video file or paste a supported video link.')

    with lock:
        jobs[jid] = {
            'id': jid, 'status': 'queued', 'progress': 3,
            'message': 'Video ready. Preparing AI analysis…',
            'clips': [], 'filename': display_name, 'clip_length': clip_length
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
