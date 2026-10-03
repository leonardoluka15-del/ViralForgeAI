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
    media_urls=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--autoplay-policy=no-user-gesture-required"]
        )
        page=browser.new_page(
            viewport={"width":1280,"height":720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
        )

        def on_request(req):
            u=req.url
            if "googlevideo.com/videoplayback" in u:
                media_urls.append(u)

        page.on("request",on_request)

        urls=[
            f"https://www.youtube.com/watch?v={video_id}",
            f"https://www.youtube.com/embed/{video_id}?autoplay=1&controls=1&rel=0"
        ]
        data=None
        for url in urls:
            try:
                page.goto(url,wait_until="domcontentloaded",timeout=90000)
                page.wait_for_timeout(7000)
                # Consent page if present
                for txt in ["Accept all","Reject all","I agree"]:
                    try:
                        page.get_by_text(txt,exact=True).click(timeout=1200)
                        page.wait_for_timeout(2500)
                    except Exception:
                        pass
                # Start playback if a player exists.
                try:
                    page.locator("video").evaluate("(v)=>{v.muted=true; return v.play()}")
                except Exception:
                    pass
                page.wait_for_timeout(8000)
                data=page.evaluate("""() => {
                  let p = window.ytInitialPlayerResponse || window.ytplayer?.config?.args?.raw_player_response || window.ytplayer?.config?.args?.player_response;
                  if (typeof p === 'string') { try { p=JSON.parse(p); } catch(e) {} }
                  return p || null;
                }""")
                if data or media_urls:
                    break
            except Exception:
                continue

        if data:
            details=data.get("videoDetails") or {}
            duration=float(details.get("lengthSeconds") or 0)
            streaming=data.get("streamingData") or {}
            fmts=streaming.get("adaptiveFormats") or []
            vids=[x for x in fmts if str(x.get("mimeType","")).startswith("video/mp4") and x.get("url")]
            auds=[x for x in fmts if str(x.get("mimeType","")).startswith("audio/mp4") and x.get("url")]
            vids=[x for x in vids if int(x.get("height") or 0)<=720] or vids
            vids.sort(key=lambda x:(int(x.get("height") or 0),int(x.get("bitrate") or 0)),reverse=True)
            auds.sort(key=lambda x:int(x.get("bitrate") or 0),reverse=True)
            if vids and auds:
                vurl=vids[0]["url"]; aurl=auds[0]["url"]
                start=max(0,min(duration-target,duration*0.12)) if duration else 0
                browser.close()
                raw=outdir/"browser_segment.mp4"
                headers="User-Agent: Mozilla/5.0\r\nReferer: https://www.youtube.com/\r\n"
                run(["ffmpeg","-y","-headers",headers,"-ss",f"{start:.2f}","-i",vurl,
                     "-headers",headers,"-ss",f"{start:.2f}","-i",aurl,
                     "-t",str(target+3),"-map","0:v:0","-map","1:a:0",
                     "-c:v","copy","-c:a","aac","-b:a","160k","-movflags","+faststart",str(raw)])
                return raw,start

        # Last browser fallback: use the media URLs Chromium itself requested.
        unique=[]
        for u in media_urls:
            if u not in unique: unique.append(u)
        browser.close()
        if unique:
            # Try media URLs pairwise. muxed URLs may work alone; adaptive URLs need video+audio.
            raw=outdir/"browser_segment.mp4"
            headers="User-Agent: Mozilla/5.0\r\nReferer: https://www.youtube.com/\r\n"
            for u in unique[:8]:
                try:
                    run(["ffmpeg","-y","-headers",headers,"-i",u,"-t",str(target+3),
                         "-c","copy","-movflags","+faststart",str(raw)])
                    if raw.exists() and raw.stat().st_size>100000:
                        return raw,0
                except Exception:
                    pass
            for i,u1 in enumerate(unique[:6]):
                for u2 in unique[i+1:6]:
                    try:
                        run(["ffmpeg","-y","-headers",headers,"-i",u1,
                             "-headers",headers,"-i",u2,"-t",str(target+3),
                             "-map","0:v:0?","-map","1:a:0?","-c:v","copy","-c:a","aac",
                             "-movflags","+faststart",str(raw)])
                        if raw.exists() and raw.stat().st_size>100000:
                            return raw,0
                    except Exception:
                        pass

        raise RuntimeError("YouTube cloud player did not expose playable media from this datacenter.")

def download_source(url,outdir,target):
    out=outdir/"source.mp4"
    attempts = [
        ["yt-dlp","--no-playlist","--js-runtimes","node",
         "-f","bv*[height<=720]+ba/b[height<=720]/b",
         "--merge-output-format","mp4","-o",str(out),url],
        ["yt-dlp","--no-playlist","--js-runtimes","node",
         "--extractor-args","youtube:player_client=web_embedded,tv_embedded",
         "-f","bv*[height<=720]+ba/b[height<=720]/b",
         "--merge-output-format","mp4","-o",str(out),url],
    ]
    last=None
    for cmd in attempts:
        try:
            run(cmd)
            return out,"yt-dlp",None
        except Exception as e:
            last=e
    m=re.search(r"(?:v=|youtu\\.be/)([A-Za-z0-9_-]{11})",url)
    if not m:
        raise last
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
