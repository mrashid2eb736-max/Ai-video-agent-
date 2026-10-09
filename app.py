import os, re, json, subprocess, asyncio, textwrap, requests
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "workspace"
for d in ["clips", "audio", "output", "thumbnails", "scripts"]:
    (DATA / d).mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

def clean_name(s):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", s.strip())[:60] or "video"

def run_cmd(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/status")
def status():
    ff = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True) if __import__("shutil").which("ffmpeg") else None
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        ollama = r.ok
        models = [x.get("name") for x in r.json().get("models", [])] if r.ok else []
    except Exception:
        ollama, models = False, []
    return jsonify({"ollama": ollama, "models": models, "ffmpeg": bool(ff), "pexels_key": bool(os.getenv("PEXELS_API_KEY")), "youtube_client": (ROOT/"client_secret.json").exists(), "facebook_token": bool(os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN"))})

@app.post("/api/generate")
def generate():
    data = request.get_json(force=True)
    topic = (data.get("topic") or "").strip()
    lang = data.get("language", "Urdu")
    fmt = data.get("format", "Shorts / Reels")
    if not topic:
        return jsonify(error="موضوع لکھیں۔"), 400
    prompt = f"""You are an ethical faceless-video production assistant. Create an original {lang} video package about: {topic}
Format: {fmt}. Return clear headings in {lang} for:
1) SEO title (max 90 characters)
2) Description (include a short disclosure if AI-generated; no fake claims)
3) 8-12 comma-separated tags
4) Narration script
5) 5-10 scene suggestions, each with a searchable stock-footage keyword
6) Thumbnail text (max 5 words)
Keep factual claims cautious, do not copy known scripts, avoid unsupported claims, and make the narration appropriate to the selected format.
"""
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    try:
        r = requests.post("http://localhost:11434/api/generate", json={"model": model, "prompt": prompt, "stream": False}, timeout=240)
        r.raise_for_status()
        answer = r.json().get("response", "").strip()
        if not answer:
            raise RuntimeError("AI model returned an empty response.")
        safe = clean_name(topic)
        (DATA/"scripts"/f"{safe}.txt").write_text(answer, encoding="utf-8")
        return jsonify(result=answer, filename=f"{safe}.txt")
    except Exception as e:
        return jsonify(error=f"Ollama سے رابطہ نہیں ہو سکا۔ Ollama کھولیں، ماڈل انسٹال کریں، پھر دوبارہ کوشش کریں۔ تفصیل: {e}"), 500

@app.post("/api/pexels")
def pexels():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify(error="کلپ تلاش کرنے کے لیے keyword لکھیں۔"), 400
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return jsonify(error="پہلے .env فائل میں PEXELS_API_KEY شامل کریں۔ مفت key: https://www.pexels.com/api/"), 400
    try:
        r = requests.get("https://api.pexels.com/v1/videos/search", headers={"Authorization": key}, params={"query": query, "per_page": 8, "orientation": data.get("orientation", "portrait")}, timeout=30)
        r.raise_for_status()
        videos = []
        for v in r.json().get("videos", []):
            files = sorted(v.get("video_files", []), key=lambda f: (f.get("width",0)*f.get("height",0)))
            good = [f for f in files if f.get("file_type") == "video/mp4" and f.get("link")]
            if not good: continue
            # Prefer a moderate resolution for 8 GB RAM
            chosen = min(good, key=lambda f: abs(max(f.get("width",0), f.get("height",0))-720))
            videos.append({"id":v.get("id"), "url":chosen["link"], "duration":v.get("duration"), "width":chosen.get("width"), "height":chosen.get("height"), "creator":v.get("user",{}).get("name","Pexels creator"), "page":v.get("url")})
        return jsonify(videos=videos, attribution="Stock videos provided by Pexels")
    except Exception as e:
        return jsonify(error=f"Pexels API error: {e}"), 500

@app.post("/api/download-clip")
def download_clip():
    data = request.get_json(force=True)
    url = data.get("url", "")
    vid = clean_name(str(data.get("id","clip")))
    if not url.startswith("https://videos.pexels.com/"):
        return jsonify(error="غیر متوقع clip URL۔ صرف Pexels video links قبول ہیں۔"), 400
    try:
        r = requests.get(url, timeout=90, stream=True)
        r.raise_for_status()
        path = DATA/"clips"/f"{vid}.mp4"
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024*256):
                if chunk: f.write(chunk)
        return jsonify(saved=str(path.relative_to(ROOT)), filename=path.name)
    except Exception as e:
        return jsonify(error=f"Download failed: {e}"), 500

@app.post("/api/voice")
def voice():
    data = request.get_json(force=True)
    script = (data.get("script") or "").strip()
    lang = data.get("language", "Urdu")
    if not script: return jsonify(error="Narration script خالی ہے۔"), 400
    voices = {"Urdu":"ur-PK-UzmaNeural", "Hindi":"hi-IN-SwaraNeural", "English":"en-US-AriaNeural"}
    voice_name = voices.get(lang, voices["Urdu"])
    out = DATA/"audio"/"narration.mp3"
    try:
        # edge-tts is a free community package using online Microsoft speech endpoints; availability may change.
        run_cmd([__import__("sys").executable, "-m", "edge_tts", "--voice", voice_name, "--text", script, "--write-media", str(out)])
        return jsonify(saved=str(out.relative_to(ROOT)), voice=voice_name)
    except Exception as e:
        return jsonify(error=f"آواز نہیں بن سکی۔ edge-tts نصب کریں اور انٹرنیٹ چیک کریں۔ تفصیل: {e}"), 500

@app.post("/api/subtitles")
def subtitles():
    data = request.get_json(force=True)
    script = (data.get("script") or "").strip()
    if not script: return jsonify(error="اسکرپٹ خالی ہے۔"), 400
    # Basic estimated timings; for exact word-level captions use speech recognition/alignment later.
    sentences = re.split(r'(?<=[.!?۔؟])\s+', script)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences: sentences=[script]
    def stamp(t):
        ms=int((t-int(t))*1000); sec=int(t)%60; mins=(int(t)//60)%60; hrs=int(t)//3600
        return f"{hrs:02d}:{mins:02d}:{sec:02d},{ms:03d}"
    cursor=0.0; blocks=[]
    for i,s in enumerate(sentences,1):
        dur=max(2.0, min(8.0, len(s.split())/2.5))
        blocks.append(f"{i}\n{stamp(cursor)} --> {stamp(cursor+dur)}\n{s}\n")
        cursor += dur
    out=DATA/"output"/"subtitles.srt"
    out.write_text("\n".join(blocks), encoding="utf-8-sig")
    return jsonify(srt="\n".join(blocks), saved=str(out.relative_to(ROOT)), note="یہ اندازاً timings ہیں؛ publish سے پہلے آواز کے مطابق چیک کریں۔")

@app.post("/api/thumbnail")
def thumbnail():
    data=request.get_json(force=True)
    title=(data.get("title") or "AI VIDEO").strip()[:90]
    try:
        from PIL import Image, ImageDraw, ImageFont
        img=Image.new("RGB",(1280,720),(15,23,42))
        draw=ImageDraw.Draw(img)
        # Background bands
        draw.rectangle((0,500,1280,720),fill=(20,70,100))
        draw.ellipse((900,-140,1400,360),fill=(28,130,140))
        draw.ellipse((-160,420,320,900),fill=(40,90,130))
        try:
            font=ImageFont.truetype("arial.ttf",72)
            small=ImageFont.truetype("arial.ttf",30)
        except:
            font=ImageFont.load_default(); small=font
        lines=textwrap.wrap(title, 22)[:3]
        y=210
        for line in lines:
            draw.text((80,y),line,font=font,fill="white",stroke_width=2,stroke_fill=(0,0,0))
            y+=95
        draw.text((82,610),"AI VIDEO AGENT",font=small,fill=(220,240,255))
        out=DATA/"thumbnails"/"thumbnail.jpg"
        img.save(out, quality=92)
        return send_file(out, as_attachment=True, download_name="thumbnail.jpg")
    except Exception as e:
        return jsonify(error=f"Thumbnail generation failed: {e}"), 500

@app.post("/api/render")
def render():
    data=request.get_json(force=True)
    title=clean_name(data.get("title") or "my_video")
    clips=sorted((DATA/"clips").glob("*.mp4"))
    audio=DATA/"audio"/"narration.mp3"
    if not clips: return jsonify(error="پہلے Pexels سے کم از کم ایک clip ڈاؤن لوڈ کریں۔"), 400
    if not audio.exists(): return jsonify(error="پہلے AI voice بنائیں۔"), 400
    out=DATA/"output"/f"{title}.mp4"
    # Lightweight render with a portrait or landscape canvas; cap at 8 clips for modest PCs.
    selected=clips[:8]
    landscape = data.get("format") == "landscape"
    width, height = (1280, 720) if landscape else (720, 1280)
    try:
        inputs=[]
        for p in selected: inputs += ["-i", str(p)]
        inputs += ["-i", str(audio)]
        n=len(selected)
        filters=[]
        for i in range(n):
            filters.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps=25,setsar=1,trim=duration=5,setpts=PTS-STARTPTS[v{i}]")
        joins="".join(f"[v{i}]" for i in range(n))
        filters.append(f"{joins}concat=n={n}:v=1:a=0[vout]")
        cmd=["ffmpeg","-y",*inputs,"-filter_complex",";".join(filters),"-map","[vout]","-map",f"{n}:a:0","-shortest","-c:v","libx264","-preset","ultrafast","-crf","28","-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)]
        run_cmd(cmd)
        return jsonify(saved=str(out.relative_to(ROOT)), note=f"Basic {width}×{height} render complete. Clips are currently 5 seconds each; review the output before publishing.")
    except Exception as e:
        return jsonify(error=f"FFmpeg render failed: {e}"), 500

@app.post("/api/youtube-upload")
def youtube_upload():
    data=request.get_json(force=True)
    path=(ROOT/data.get("path","")).resolve()
    if not path.is_file() or not str(path).startswith(str((DATA/"output").resolve())):
        return jsonify(error="Output folder سے درست ویڈیو فائل منتخب کریں۔"), 400
    if not (ROOT/"client_secret.json").exists():
        return jsonify(error="Google Cloud سے OAuth Desktop client JSON ڈاؤن لوڈ کرکے app folder میں client_secret.json نام سے رکھیں۔ README دیکھیں۔"), 400
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request
        import pickle
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
        token=ROOT/"youtube_token.pickle"
        creds=None
        if token.exists():
            with open(token,"rb") as f: creds=pickle.load(f)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
            else: creds=InstalledAppFlow.from_client_secrets_file(str(ROOT/"client_secret.json"),scopes).run_local_server(port=0)
            with open(token,"wb") as f: pickle.dump(creds,f)
        yt=build("youtube","v3",credentials=creds)
        body={"snippet":{"title":data.get("title","AI Video")[:100],"description":data.get("description",""),"tags":[x.strip() for x in data.get("tags","").split(",") if x.strip()][:500],"categoryId":"22"},"status":{"privacyStatus":data.get("privacy","private")}}
        req=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(path),mimetype="video/mp4",resumable=True))
        response=req.execute()
        return jsonify(ok=True, video_id=response.get("id"), url="https://youtube.com/watch?v="+response.get("id",""), note="Unverified Google API projects may be restricted to private uploads.")
    except Exception as e:
        return jsonify(error=f"YouTube upload error: {e}"), 500

@app.post("/api/facebook-upload")
def facebook_upload():
    data=request.get_json(force=True)
    path=(ROOT/data.get("path","")).resolve()
    if not path.is_file() or not str(path).startswith(str((DATA/"output").resolve())):
        return jsonify(error="Output folder سے درست ویڈیو فائل منتخب کریں۔"), 400
    page_id=os.getenv("FACEBOOK_PAGE_ID"); token=os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
    if not page_id or not token:
        return jsonify(error=".env میں FACEBOOK_PAGE_ID اور FACEBOOK_PAGE_ACCESS_TOKEN شامل کریں۔ README دیکھیں۔"), 400
    try:
        with open(path,"rb") as f:
            r=requests.post(f"https://graph.facebook.com/v23.0/{page_id}/videos",data={"access_token":token,"title":data.get("title","AI Video"),"description":data.get("description","")},files={"source":(path.name,f,"video/mp4")},timeout=900)
        r.raise_for_status()
        return jsonify(ok=True, response=r.json(), note="Meta API version/permissions may change; test with a short video first.")
    except Exception as e:
        return jsonify(error=f"Facebook upload error: {e}"), 500

@app.get("/api/files")
def files():
    out=[]
    for folder in ["clips","audio","output","thumbnails","scripts"]:
        for p in sorted((DATA/folder).glob("*")):
            if p.is_file(): out.append({"name":p.name,"path":str(p.relative_to(ROOT)).replace("\\","/"),"folder":folder,"bytes":p.stat().st_size})
    return jsonify(files=out)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
