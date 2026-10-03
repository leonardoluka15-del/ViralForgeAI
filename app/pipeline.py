from pathlib import Path
import subprocess, re
import numpy as np
import cv2

HOOK_WORDS = {
    'secret':2.3,'crazy':2.0,'insane':2.1,'never':1.6,'best':1.4,'worst':1.4,
    'why':1.5,'how':1.3,'truth':2.0,'mistake':1.8,'shocking':2.2,'unbelievable':2.0,
    'actually':1.2,'wait':1.8,'watch':1.6,'listen':1.5,'money':1.5,'million':1.8,
    'free':1.4,'win':1.5,'lost':1.5,'problem':1.2,'hack':1.8,'warning':1.7,
    'amazing':1.7,'impossible':1.7,'finally':1.4,'breaking':1.8,'nobody':1.4,
    'everyone':1.2,'first':1.0,'biggest':1.5,'exactly':1.1,'stop':1.3
}

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout

def duration(src):
    out = run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(src)])
    return float(out.strip())

def transcribe(src, update):
    update(12, 'Transcribing speech with Whisper AI…')
    from faster_whisper import WhisperModel
    model = WhisperModel('base', device='cpu', compute_type='int8')
    segs, _ = model.transcribe(str(src), vad_filter=True, word_timestamps=True, beam_size=3)
    data=[]
    for s in segs:
        txt=s.text.strip()
        if txt:
            data.append({'start':float(s.start),'end':float(s.end),'text':txt})
    return data

def visual_energy(src, update):
    update(33, 'Detecting motion, cuts and visual peaks…')
    cap=cv2.VideoCapture(str(src))
    fps=cap.get(cv2.CAP_PROP_FPS) or 25.0
    sample=max(1,int(fps*0.75))
    prev=None; i=0; scores=[]
    while True:
        ok,frame=cap.read()
        if not ok: break
        if i%sample==0:
            gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
            gray=cv2.resize(gray,(192,108))
            val=0.0 if prev is None else float(np.mean(cv2.absdiff(gray,prev)))
            scores.append((i/fps,val)); prev=gray
        i+=1
    cap.release()
    return scores

def sentence_score(text):
    low=text.lower()
    words=re.findall(r"[a-zA-Z']+",low)
    if not words: return 0.0
    score=sum(HOOK_WORDS.get(w,0) for w in words)
    score += min(len(words)/22,2.4)
    score += 1.4 if '?' in text else 0
    score += 1.0 if '!' in text else 0
    score += min(len(re.findall(r'\b\d+(?:\.\d+)?\b',text))*0.5,1.5)
    if len(words) <= 18: score += .6
    return score

def candidates(total,segs,clip_length):
    out=[]
    if segs:
        for s in segs:
            start=max(0,s['start']-3)
            end=min(total,start+clip_length)
            if end-start<min(12,clip_length*.65): continue
            txt=' '.join(x['text'] for x in segs if x['end']>=start and x['start']<=end)
            out.append({'start':start,'end':end,'text':txt})
    else:
        step=max(10,clip_length//2); t=0
        while t<total-8:
            out.append({'start':t,'end':min(total,t+clip_length),'text':''}); t+=step
    dedup=[]
    for x in out:
        if not dedup or abs(x['start']-dedup[-1]['start'])>6:
            dedup.append(x)
    return dedup

def reason_for(text, visual, start, end):
    reasons=[]; low=text.lower()
    if any(w in low for w in ['why','how','secret','truth','mistake','wait','never']): reasons.append('strong verbal hook')
    if '?' in text: reasons.append('curiosity gap')
    if '!' in text: reasons.append('high-emotion delivery')
    vis=[v for t,v in visual if start<=t<=end]
    if vis and np.mean(vis)>np.mean([v for _,v in visual] or [0]): reasons.append('above-average visual activity')
    if len(text.split())>35: reasons.append('dense spoken content')
    return ', '.join(reasons[:3]) or 'good pacing and self-contained context'

def rank(cands, visual):
    vals=np.array([v for _,v in visual],dtype=float) if visual else np.array([0.])
    scale=max(float(np.percentile(vals,90)),1.0)
    scored=[]
    for c in cands:
        vis=[v for t,v in visual if c['start']<=t<=c['end']]
        motion=min((float(np.mean(vis))/scale)*4.2,5.5) if vis else 0
        text=sentence_score(c['text'])
        hook=sentence_score(' '.join(c['text'].split()[:20]))*0.85
        completeness=.9 if c['text'][-1:] in '.!?' else .25
        raw=text+hook+motion+completeness
        d=dict(c); d['raw']=raw; scored.append(d)
    scored.sort(key=lambda x:x['raw'],reverse=True)
    picks=[]
    for s in scored:
        if all(min(s['end'],p['end'])-max(s['start'],p['start'])<5 for p in picks):
            picks.append(s)
        if len(picks)>=6: break
    if not picks: return []
    hi=max(p['raw'] for p in picks); lo=min(p['raw'] for p in picks); span=max(hi-lo,.01)
    for i,p in enumerate(picks):
        p['score']=int(round(74+24*(p['raw']-lo)/span))
        p['rank']=i+1
        p['reason']=reason_for(p['text'], visual, p['start'], p['end'])
    return picks

def srt_time(sec):
    ms=max(0,int(round(sec*1000))); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'

def write_srt(path,segs,start,end):
    rows=[]; n=1
    for s in segs:
        if s['end']<start or s['start']>end: continue
        a=max(s['start'],start)-start; b=min(s['end'],end)-start
        words=s['text'].split()
        chunks=[' '.join(words[i:i+7]) for i in range(0,len(words),7)] or [s['text']]
        dur=max(b-a,.2); each=dur/len(chunks)
        for j,ch in enumerate(chunks):
            ca=a+j*each; cb=min(b,ca+each)
            rows += [str(n), f'{srt_time(ca)} --> {srt_time(cb)}', ch.upper(), '']; n+=1
    path.write_text('\n'.join(rows),encoding='utf-8')

def esc(p):
    return str(p).replace('\\','/').replace(':','\\:').replace("'","\\'")

def export(src,out,start,end,srt):
    vf="scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"
    if srt.exists() and srt.stat().st_size:
        vf += f",subtitles='{esc(srt)}':force_style='FontName=Arial,FontSize=19,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=0,Alignment=2,MarginV=155'"
    run(['ffmpeg','-y','-ss',f'{start:.3f}','-i',str(src),'-t',f'{end-start:.3f}','-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','21','-c:a','aac','-b:a','160k','-movflags','+faststart',str(out)])

def process_video(src,outdir,update,clip_length=35):
    outdir.mkdir(parents=True,exist_ok=True)
    update(6,'Inspecting video…')
    total=duration(src)
    segs=transcribe(src,update)
    vis=visual_energy(src,update)
    update(53,'AI is scoring hooks, pacing and visual energy…')
    picks=rank(candidates(total,segs,clip_length),vis)
    if not picks:
        picks=[{'start':0,'end':min(total,clip_length),'text':'AI-selected opening moment','score':72,'rank':1,'reason':'fallback selection'}]
    clips=[]
    for i,p in enumerate(picks,1):
        update(58+int(36*i/max(1,len(picks))),f'Rendering vertical clip {i} of {len(picks)}…')
        srt=outdir/f'clip_{i}.srt'; write_srt(srt,segs,p['start'],p['end'])
        mp4=outdir/f'viral_clip_{i}.mp4'; export(src,mp4,p['start'],p['end'],srt)
        title=p['text'].strip().replace('\n',' ')
        if len(title)>100: title=title[:97].rsplit(' ',1)[0]+'…'
        clips.append({'rank':i,'viral_score':p['score'],'start':round(p['start'],1),'end':round(p['end'],1),'title':title or f'AI pick #{i}','reason':p['reason'],'url':f'/media/{outdir.name}/{mp4.name}'})
    update(98,'Finalizing clips…')
    return clips
