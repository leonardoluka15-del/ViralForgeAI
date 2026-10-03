from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import uuid, shutil, threading, urllib.parse, urllib.request, os, secrets, subprocess, re
from datetime import datetime, timezone
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
content_queue = []
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

@app.get('/api/trends')
def youtube_trends(region: str = 'US', max_results: int = 24):
    region = (region or 'US').upper()[:2]
    max_results = max(6, min(int(max_results or 24), 40))
    try:
        yt = build('youtube','v3',credentials=youtube_credentials(),cache_discovery=False)
        resp = yt.videos().list(
            part='snippet,statistics,contentDetails',
            chart='mostPopular',
            regionCode=region,
            maxResults=max_results
        ).execute()
        now = datetime.now(timezone.utc)
        rows = []
        for item in resp.get('items',[]):
            sn = item.get('snippet',{})
            st = item.get('statistics',{})
            published = sn.get('publishedAt')
            try:
                dt = datetime.fromisoformat(published.replace('Z','+00:00')) if published else now
                age_hours = max((now-dt).total_seconds()/3600, 0.5)
            except Exception:
                age_hours = 24.0
            views = int(st.get('viewCount') or 0)
            likes = int(st.get('likeCount') or 0)
            comments = int(st.get('commentCount') or 0)
            vph = int(views/age_hours) if age_hours else views
            engagement = ((likes+comments)/max(views,1))*100
            title = sn.get('title','')
            category = sn.get('categoryId','')
            rows.append({
                'video_id': item.get('id'),
                'title': title,
                'description': (sn.get('description') or '')[:1200],
                'channel': sn.get('channelTitle'),
                'published_at': published,
                'age_hours': round(age_hours,1),
                'views': views,
                'views_per_hour': vph,
                'likes': likes,
                'comments': comments,
                'engagement_pct': round(engagement,2),
                'category_id': category,
                'thumbnail': ((sn.get('thumbnails') or {}).get('high') or (sn.get('thumbnails') or {}).get('medium') or (sn.get('thumbnails') or {}).get('default') or {}).get('url'),
                'url': f"https://www.youtube.com/watch?v={item.get('id')}",
            })
        rows.sort(key=lambda x:(x['views_per_hour'],x['views']),reverse=True)
        for i,row in enumerate(rows,1):
            row['rank']=i
            # heuristic opportunity score, capped 100
            velocity=min(row['views_per_hour']/50000,1.0)
            engagement=min(row['engagement_pct']/8.0,1.0)
            freshness=max(0.0,1.0-min(row['age_hours']/96.0,1.0))
            row['trend_score']=int(round(100*(0.58*velocity+0.22*engagement+0.20*freshness)))
        return {'region':region,'count':len(rows),'items':rows}
    except Exception as e:
        raise HTTPException(500, f'Trend Scout failed: {str(e)[:300]}')

@app.post('/api/queue/from-trend')
async def queue_from_trend(
    video_id: str = Form(...),
    title: str = Form(...),
    channel: str = Form(''),
    views: int = Form(0),
    views_per_hour: int = Form(0),
    trend_score: int = Form(0),
    source_url: str = Form('')
):
    qid = uuid.uuid4().hex[:10]
    clean_title = (title or '').strip()
    low = clean_title.lower()

    if any(k in low for k in ['trailer','teaser','movie','film','netflix','marvel','dc','disney','prime video']):
        angle = 'Explain why this trailer is trending and highlight the most talked-about reveal without reposting the full trailer.'
        hook = f"Everyone is talking about {clean_title[:70]} — here’s the moment people noticed."
    elif any(k in low for k in ['mrbeast','challenge','giveaway','survive','million']):
        angle = 'Create a commentary-style recap focused on the surprising premise, stakes, and audience reaction.'
        hook = f"This is why {clean_title[:70]} is exploding right now."
    elif any(k in low for k in ['game','gaming','playstation','xbox','nintendo','fortnite','gta','minecraft']):
        angle = 'Create a fast gaming-news Short explaining the announcement and why fans care.'
        hook = f"Gamers are reacting fast to {clean_title[:70]}."
    else:
        angle = 'Create a short commentary recap explaining what happened, why it is trending, and the most interesting takeaway.'
        hook = f"This video is taking off fast — here’s why: {clean_title[:70]}"

    generated_title = clean_title[:88] if clean_title else 'Trending video explained'
    if len(generated_title) < 90 and not generated_title.endswith('...'):
        generated_title = generated_title.rstrip(' .') + ' — Why It’s Trending'

    description = f"{angle}\n\nSource: {channel or 'YouTube'}\n#Shorts #Trending #Viral"
    hashtags = ['#Shorts','#Trending','#Viral']
    if channel:
        safe_tag = ''.join(ch for ch in channel.title().replace(' ','') if ch.isalnum())
        if safe_tag:
            hashtags.append('#'+safe_tag[:24])

    item = {
        'id': qid,
        'status': 'idea_ready',
        'source_video_id': video_id,
        'source_url': source_url or f"https://www.youtube.com/watch?v={video_id}",
        'source_title': clean_title,
        'source_channel': channel,
        'views': int(views or 0),
        'views_per_hour': int(views_per_hour or 0),
        'trend_score': int(trend_score or 0),
        'hook': hook,
        'angle': angle,
        'title': generated_title[:100],
        'description': description,
        'hashtags': hashtags,
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    with lock:
        content_queue.insert(0,item)
        del content_queue[50:]
    return item

def _queue_lookup(queue_id):
    return next((x for x in content_queue if x.get('id') == queue_id), None)

def _safe_text(s, limit=220):
    s = re.sub(r'\\s+', ' ', (s or '')).strip()
    s = re.sub(r'https?://\\S+', '', s)
    return s[:limit].strip()

def _make_trend_script(item, source_description=''):
    title = _safe_text(item.get('source_title'), 120)
    channel = _safe_text(item.get('source_channel') or 'a YouTube channel', 80)
    desc = _safe_text(source_description, 260)
    vph = int(item.get('views_per_hour') or 0)
    parts = [
        item.get('hook') or f"This is trending fast: {title}.",
        f"The video comes from {channel} and is currently gaining about {vph:,} views per hour."
    ]
    if desc:
        parts.append(f"According to the video's description: {desc}")
    parts.append("That momentum is why this topic is showing up in ViralForge Trend Scout right now.")
    return ' '.join(parts)

def _write_simple_srt(path, script, duration):
    words = script.split()
    chunks = [' '.join(words[i:i+7]) for i in range(0,len(words),7)] or [script]
    def ts(sec):
        ms=max(0,int(sec*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    rows=[]; step=max(duration/max(len(chunks),1),0.8)
    for i,ch in enumerate(chunks,1):
        a=(i-1)*step; b=min(duration,i*step)
        rows += [str(i), f"{ts(a)} --> {ts(b)}", ch.upper(), '']
    path.write_text('\\n'.join(rows), encoding='utf-8')

def _run_trend_generation(queue_id):
    with lock:
        item = _queue_lookup(queue_id)
        if not item:
            return
        item['status'] = 'generating'
    outdir = OUTPUTS / f"trend_{queue_id}"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        yt = build('youtube','v3',credentials=youtube_credentials(),cache_discovery=False)
        meta = yt.videos().list(part='snippet', id=item['source_video_id']).execute()
        sn = (meta.get('items') or [{}])[0].get('snippet',{})
        description = sn.get('description') or ''
        thumb = ((sn.get('thumbnails') or {}).get('maxres') or (sn.get('thumbnails') or {}).get('high') or (sn.get('thumbnails') or {}).get('medium') or {}).get('url')
        if not thumb:
            raise RuntimeError('No usable thumbnail was available for this trend.')

        image_path = outdir / 'source_thumb.jpg'
        urllib.request.urlretrieve(thumb, image_path)

        script = _make_trend_script(item, description)
        audio_path = outdir / 'narration.mp3'
        tts = subprocess.run(
            ['edge-tts','--voice','en-US-AriaNeural','--text',script,'--write-media',str(audio_path)],
            capture_output=True, text=True, timeout=90
        )
        if tts.returncode != 0:
            raise RuntimeError((tts.stderr or tts.stdout or 'Text-to-speech failed')[-500:])

        probe = subprocess.run(
            ['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(audio_path)],
            capture_output=True,text=True,timeout=30
        )
        dur=max(float((probe.stdout or '20').strip()),6.0)
        srt_path=outdir/'captions.srt'
        _write_simple_srt(srt_path,script,dur)

        final_path=outdir/'trend_short.mp4'
        base_path = outdir/'trend_short_base.mp4'
        base_vf = (
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,"
            "boxblur=18:10,"
            "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.32:t=fill"
        )
        ff = subprocess.run(
            ['ffmpeg','-y','-loop','1','-i',str(image_path),'-i',str(audio_path),
             '-t',f'{dur:.2f}','-vf',base_vf,'-r','30','-c:v','libx264','-preset','veryfast',
             '-crf','23','-c:a','aac','-b:a','128k','-shortest','-movflags','+faststart',str(base_path)],
            capture_output=True,text=True,timeout=180
        )
        if ff.returncode != 0:
            raise RuntimeError((ff.stderr or ff.stdout or 'Base FFmpeg render failed')[-1200:])

        # Burn captions as a second pass. If libass/font rendering fails on the host,
        # preserve the base narrated Short instead of failing the whole job.
        srt_ffmpeg = str(srt_path).replace('\\','/')
        caption_vf = (
            f"subtitles={srt_ffmpeg}:"
            "force_style='FontName=Arial,FontSize=18,Bold=1,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Alignment=2,MarginV=150'"
        )
        cap = subprocess.run(
            ['ffmpeg','-y','-i',str(base_path),'-vf',caption_vf,
             '-c:v','libx264','-preset','veryfast','-crf','23',
             '-c:a','copy','-movflags','+faststart',str(final_path)],
            capture_output=True,text=True,timeout=180
        )
        if cap.returncode != 0:
            shutil.copyfile(base_path, final_path)

        with lock:
            item = _queue_lookup(queue_id)
            if item:
                item['status']='video_ready'
                item['script']=script
                item['media_url']=f"/media/trend_{queue_id}/trend_short.mp4"
    except Exception as e:
        with lock:
            item = _queue_lookup(queue_id)
            if item:
                item['status']='error'
                item['error']=str(e)[:500]

@app.post('/api/queue/{queue_id}/generate')
def generate_queue_video(queue_id: str, background_tasks: BackgroundTasks):
    with lock:
        item = _queue_lookup(queue_id)
        if not item:
            raise HTTPException(404,'Queue item not found.')
        if item.get('status') == 'generating':
            return {'ok':True,'status':'generating'}
        item['status']='queued_for_generation'
        item.pop('error',None)
    background_tasks.add_task(_run_trend_generation, queue_id)
    return {'ok':True,'status':'queued_for_generation'}

@app.get('/api/queue')
def get_queue():
    with lock:
        return {'count': len(content_queue), 'items': list(content_queue)}

@app.delete('/api/queue/{queue_id}')
def delete_queue_item(queue_id: str):
    with lock:
        before=len(content_queue)
        content_queue[:] = [x for x in content_queue if x.get('id') != queue_id]
        return {'ok': len(content_queue) < before}

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
