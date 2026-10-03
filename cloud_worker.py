import os, json, tempfile, subprocess, re, html
from pathlib import Path
from urllib.parse import urlparse
import requests

API=os.getenv("VIRALFORGE_URL","https://viralforge-ai-jwuo.onrender.com").rstrip("/")

HOOKS={"secret":3,"crazy":2,"insane":2,"never":1.5,"why":1.8,"how":1.3,"truth":2,
       "mistake":2,"shocking":2.5,"unbelievable":2,"wait":2,"watch":1.8,"million":2,
       "impossible":2,"biggest":1.7,"first":1.2,"revealed":2,"reveal":2,"finally":1.4}

def run(cmd):
    p=subprocess.run(cmd,capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError((p.stderr or p.stdout)[-4000:])
    return p.stdout

def pick_caption_window(events,target,duration):
    segs=[]
    for e in events or []:
        if "tStartMs" not in e: continue
        text="".join(s.get("utf8","") for s in e.get("segs",[])).replace("\n"," ").strip()
        if not text: continue
        segs.append((float(e["tStartMs"])/1000.0,text))
    if not segs:
        return max(0,min(duration-target,duration*0.12))
    best=(0.0,-1.0)
    for st,_ in segs:
        end=st+target
        text=" ".join(t for ts,t in segs if st<=ts<=end)
        words=re.findall(r"[A-Za-z']+",text.lower())
        score=sum(HOOKS.get(w,0) for w in words)
        score+=1.5*text.count("?")+0.8*text.count("!")
        score+=min(len(words)/35,3)
        if score>best[1]: best=(st,score)
    return max(0,min(best[0]-1.5,max(0,duration-target)))

def browser_segment(video_id,outdir,target):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--autoplay-policy=no-user-gesture-required"]
        )
        page=browser.new_page(
            viewport={"width":1280,"height":720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
        )
        url=f"https://www.youtube.com/embed/{video_id}?autoplay=1&controls=0&rel=0"
        page.goto(url,wait_until="domcontentloaded",timeout=90000)
        page.wait_for_timeout(5000)
        data=page.evaluate("""() => {
          let p = window.ytInitialPlayerResponse || window.ytplayer?.config?.args?.raw_player_response || window.ytplayer?.config?.args?.player_response;
          if (typeof p === 'string') { try { p=JSON.parse(p); } catch(e) {} }
          return p || null;
        }""")
        if not data:
            raise RuntimeError("YouTube browser player did not expose player response.")
        details=data.get("videoDetails") or {}
        duration=float(details.get("lengthSeconds") or 0)
        streaming=data.get("streamingData") or {}
        fmts=streaming.get("adaptiveFormats") or []
        vids=[x for x in fmts if str(x.get("mimeType","")).startswith("video/mp4") and x.get("url")]
        auds=[x for x in fmts if str(x.get("mimeType","")).startswith("audio/mp4") and x.get("url")]
        vids=[x for x in vids if int(x.get("height") or 0)<=720] or vids
        vids.sort(key=lambda x:(int(x.get("height") or 0),int(x.get("bitrate") or 0)),reverse=True)
        auds.sort(key=lambda x:int(x.get("bitrate") or 0),reverse=True)
        if not vids or not auds:
            raise RuntimeError("Browser player did not provide direct MP4 stream URLs.")

        events=[]
        try:
            tracks=(((data.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or [])
            if tracks:
                cap_url=tracks[0].get("baseUrl")
                if cap_url:
                    sep="&" if "?" in cap_url else "?"
                    txt=page.evaluate("""async (u) => { const r=await fetch(u); return await r.text(); }""",cap_url+sep+"fmt=json3")
                    events=(json.loads(txt) or {}).get("events") or []
        except Exception:
            events=[]

        start=pick_caption_window(events,target,duration or target*3)
        vurl=vids[0]["url"]; aurl=auds[0]["url"]
        browser.close()

    raw=outdir/"browser_segment.mp4"
    headers="User-Agent: Mozilla/5.0\r\nReferer: https://www.youtube.com/\r\n"
    cmd=["ffmpeg","-y","-headers",headers,"-ss",f"{start:.2f}","-i",vurl,
         "-headers",headers,"-ss",f"{start:.2f}","-i",aurl,
         "-t",str(target+3),"-map","0:v:0","-map","1:a:0",
         "-c:v","copy","-c:a","aac","-b:a","160k","-movflags","+faststart",str(raw)]
    run(cmd)
    return raw,start

def download_source(url,outdir,target):
    out=outdir/"source.mp4"
    try:
        run(["yt-dlp","--no-playlist",
             "-f","bv*[height<=720]+ba/b[height<=720]/b",
             "--merge-output-format","mp4","-o",str(out),url])
        return out,"yt-dlp",None
    except Exception as e:
        m=re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",url)
        if not m:
            raise
        seg,start=browser_segment(m.group(1),outdir,target)
        return seg,"browser",start

def post_progress(job_id,status,message):
    try:
        requests.post(f"{API}/api/worker/{job_id}/progress",
                      data={"status":status,"message":message},timeout=30)
    except Exception:
        pass

def main():
    r=requests.get(f"{API}/api/worker/next",timeout=60)
    r.raise_for_status()
    job=r.json()
    if not job or not job.get("id"):
        print("No queued trend.")
        return

    jid=job["id"]; url=job["source_url"]; target=int(job.get("target_length") or 30)

    try:
        with tempfile.TemporaryDirectory() as td:
            td=Path(td)
            post_progress(jid,"cloud_downloading","Cloud worker is fetching the actual source video.")
            src,method,start=download_source(url,td,target)
            post_progress(jid,"cloud_analyzing",f"Actual footage fetched via {method}. AI is selecting the strongest moment.")

            from app.pipeline import process_video
            out=td/"out"
            def update(p,m): post_progress(jid,"cloud_analyzing",m)
            clips=process_video(src,out,update,clip_length=target,max_clips=1)
            if not clips: raise RuntimeError("AI did not produce a clip.")
            best=out/"viral_clip_1.mp4"
            if not best.exists(): best=next(out.glob("*.mp4"))

            post_progress(jid,"cloud_uploading","Uploading finished AI clip back to ViralForge.")
            with best.open("rb") as fh:
                rr=requests.post(f"{API}/api/worker/{jid}/complete",
                    files={"video":("viral_short.mp4",fh,"video/mp4")},timeout=240)
                rr.raise_for_status()
            print(json.dumps(rr.json()))
    except Exception as e:
        post_progress(jid,"error",str(e)[-500:])
        raise

if __name__=="__main__":
    main()
