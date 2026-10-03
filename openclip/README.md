# OpenClip Render

Render-hosted AI clipping suite.

## Render configuration
Build command:
`pip install -r openclip/requirements.txt`

Start command:
`cd openclip && uvicorn app:app --host 0.0.0.0 --port $PORT`

Environment:
- WHISPER_MODEL=tiny
- OLLAMA_MODEL=llama3.2:3b
- OLLAMA_BASE_URL=<public Ollama endpoint when connected>

Without an Ollama endpoint the app stays usable with heuristic scoring. Once an accessible Ollama endpoint is configured, the same workflow automatically uses Llama 3.2 3B.

Media on the free Render filesystem is ephemeral, so download generated clips after rendering.
