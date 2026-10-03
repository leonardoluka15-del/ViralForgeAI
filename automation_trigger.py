import os
import sys
import requests

BASE=os.environ.get("VIRALFORGE_URL","https://viralforge-ai-jwuo.onrender.com").rstrip("/")
SECRET=os.environ["AUTOMATION_SECRET"]

r=requests.post(
    f"{BASE}/api/automation/run-once",
    headers={"Authorization":f"Bearer {SECRET}","Content-Type":"application/json"},
    json={},
    timeout=90,
)
print("status",r.status_code)
print(r.text)
r.raise_for_status()
