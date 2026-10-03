# Viral Clipper — Local AI Video Clipping

A local end-to-end web app for turning long videos into short vertical clips.

## Features
- Upload local video or paste a supported public video URL
- Faster-Whisper transcription on CPU
- Viral-window candidate detection
- Ollama + `llama3.2:3b` ranking when available
- Automatic heuristic fallback when Ollama is offline
- 9:16 1080×1920 rendering
- Burned-in subtitles
- H.264 CRF 18 quality output + AAC 192 kbps audio
- Preview and direct MP4 downloads
- No paid API required

## Windows setup
1. Start **Docker Desktop** and wait until the engine says it is running.
2. Start **Ollama** on Windows and make sure the model exists:
   ```powershell
   ollama pull llama3.2:3b
   ollama serve
   ```
   If Ollama is already running, do not start a second copy.
3. Open PowerShell in this project folder and run:
   ```powershell
   docker compose up --build
   ```
4. Open:
   `http://localhost:3000`

## Stop
```powershell
docker compose down
```

## Notes
- The first transcription can take longer because the Whisper model is downloaded automatically into the container.
- URL import uses yt-dlp and only works where the source site permits access. Upload is the most reliable mode.
- The app renders portrait output at 1080×1920 with CRF 18.
