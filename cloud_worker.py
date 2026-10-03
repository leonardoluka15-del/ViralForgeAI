import os, json, tempfile, subprocess, shutil
from pathlib import Path
import requests

API=os.getenv("VIRALFORGE_URL","https://viralforge-ai-jwuo.onrender.com").rstrip("/")

def run(cmd):
    p=subprocess.run(cmd,capture_output=True,text=True)
    if p.returncode!=0:
        raise RuntimeError((p.stderr or p.stdout)[-3000:])
    return p.stdout

def main():
    r=requests.get(f"{API}/api/worker/next",timeout=45)
    r.raise_for_status()
    job=r.json()
    if not job or not job.get("id"):
        print("No queued trend.")
        return

    jid=job["id"]
    url=job["source_url"]
    target=int(job.get("target_length") or 30)

    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        src=td/"source.mp4"
        requests.post(f"{API}/api/worker/{jid}/progress",
                      data={"status":"cloud_downloading","message":"Cloud worker is fetching the source video."},timeout=30)

        run(["yt-dlp","--no-playlist",
             "-f","bv*[height<=720]+ba/b[height<=720]/b",
             "--merge-output-format","mp4","-o",str(src),url])

        requests.post(f"{API}/api/worker/{jid}/progress",
                      data={"status":"cloud_analyzing","message":"Cloud AI is finding the strongest moment."},timeout=30)

        from app.pipeline import process_video
        out=td/"out"
        def update(p,m):
            requests.post(f"{API}/api/worker/{jid}/progress",
                          data={"status":"cloud_analyzing","message":m},timeout=30)
        clips=process_video(src,out,update,clip_length=target,max_clips=1)
        if not clips:
            raise RuntimeError("AI did not produce a clip.")
        best=out/"viral_clip_1.mp4"
        if not best.exists():
            best=next(out.glob("*.mp4"))

        requests.post(f"{API}/api/worker/{jid}/progress",
                      data={"status":"cloud_uploading","message":"Uploading finished Short back to ViralForge."},timeout=30)
        with best.open("rb") as f:
            rr=requests.post(f"{API}/api/worker/{jid}/complete",
                             files={"video":("viral_short.mp4",f,"video/mp4")},timeout=180)
            rr.raise_for_status()
        print(rr.json())

if __name__=="__main__":
    main()
