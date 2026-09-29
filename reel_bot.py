import base64, json, os, subprocess, sys, tempfile, time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
GRAPH = "https://graph.facebook.com/v21.0"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SHOT_SECONDS = 3.5
RUN = os.getenv("GITHUB_RUN_ID", "local")
OUT = Path("out")


# ---------- 1. Extract ----------
def fetch_html(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.ok and len(r.text) > 5000:
            return r.text
    except requests.RequestException:
        pass
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA)
        pg.goto(url, wait_until="networkidle", timeout=45000)
        html = pg.content()
        b.close()
    return html


def extract_product(url):
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    prod = {"title": "", "price": "", "description": "", "images": []}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "Product":
                prod["title"] = item.get("name", "")
                prod["description"] = item.get("description", "")
                img = item.get("image", [])
                prod["images"] = [img] if isinstance(img, str) else list(img)
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                prod["price"] = str(offers.get("price", ""))
    og = lambda k: (soup.find("meta", property=k) or {}).get("content", "")
    prod["title"] = prod["title"] or og("og:title")
    prod["description"] = prod["description"] or og("og:description")
    if not prod["images"] and og("og:image"):
        prod["images"] = [og("og:image")]
    if not prod["title"] or not prod["images"]:
        raise RuntimeError("Could not read product data from this page.")
    prod["images"] = prod["images"][:3]
    return prod


# ---------- 2. Gemini (free tier) ----------
def gemini(model, parts, cfg=None):
    r = requests.post(f"{GEMINI}/{model}:generateContent",
                      headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                      json={"contents": [{"parts": parts}],
                            "generationConfig": cfg or {}}, timeout=120)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"]


def plan_styling(prod):
    prompt = f"""You are a stylist making an Instagram Reel.
Product: {prod['title']} | Price: {prod['price']}
Details: {prod['description'][:800]}
Return ONLY JSON: {{"accessories": [3 items that truly match],
"shots": [4 x {{"edit_prompt": "edit instruction that keeps the ORIGINAL
PRODUCT UNCHANGED, only adds accessories/background/lighting",
"overlay": "max 6 words on-screen text"}}],
"caption": "engaging, under 300 chars, no false claims",
"hashtags": [8 relevant hashtags]}}"""
    parts = gemini("gemini-2.5-flash", [{"text": prompt}],
                   {"responseMimeType": "application/json"})
    return json.loads(parts[0]["text"])


def styled_image(img_bytes, prompt):
    """Try free Gemini image editing; return bytes or None."""
    try:
        parts = gemini("gemini-2.5-flash-image", [
            {"text": prompt + " Vertical 9:16, photorealistic. "
                              "Do not alter the product itself."},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(img_bytes).decode()}}])
        for p in parts:
            d = p.get("inlineData") or p.get("inline_data")
            if d:
                return base64.b64decode(d["data"])
    except Exception as e:
        print("  image gen unavailable, using original photo:", str(e)[:80])
    return None


# ---------- 3. Video ----------
def build_video(shots, plan, work):
    frames = int(SHOT_SECONDS * 30)
    clips = []
    for i, img in enumerate(shots):
        txt = work / f"t{i}.txt"
        txt.write_text(plan["shots"][i % len(plan["shots"])]["overlay"])
        clip = work / f"clip{i}.mp4"
        vf = (f"scale=2160:3840:force_original_aspect_ratio=increase,"
              f"crop=2160:3840,"
              f"zoompan=z='min(zoom+0.0007,1.12)':d={frames}:s=1080x1920:fps=30,"
              f"drawtext=fontfile={FONT}:textfile={txt}:fontcolor=white:"
              f"fontsize=68:box=1:boxcolor=black@0.45:boxborderw=24:"
              f"x=(w-text_w)/2:y=h*0.78,format=yuv420p")
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-vf", vf,
                        "-t", str(SHOT_SECONDS), "-r", "30", "-c:v", "libx264",
                        str(clip)], check=True, capture_output=True)
        clips.append(clip)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{c}'\n" for c in clips))
    final = OUT / f"reel_{RUN}.mp4"
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
    if Path("music.mp3").exists():
        cmd += ["-i", "music.mp3", "-map", "0:v", "-map", "1:a", "-shortest",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(final)]
    subprocess.run(cmd, check=True, capture_output=True)
    return final


def build(url):
    OUT.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp())
    print("Extracting..."); prod = extract_product(url)
    print("Planning..."); plan = plan_styling(prod)
    print("Creating shots...")
    shots = []
    for src in prod["images"] * 4:
        if len(shots) == 4:
            break
        raw = requests.get(src, headers={"User-Agent": UA}, timeout=30).content
        gen = styled_image(raw, plan["shots"][len(shots)]["edit_prompt"])
        p = work / f"shot{len(shots)}.jpg"
        p.write_bytes(gen or raw)
        shots.append(p)
    print("Rendering...")
    video = build_video(shots, plan, work)
    caption = (plan["caption"] + "\n\n" + " ".join(plan["hashtags"])
               + "\n\n#ad AI-assisted visuals")
    (OUT / f"reel_{RUN}.txt").write_text(caption)
    print("Built", video)


# ---------- 4. Publish ----------
def publish():
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.getenv("GITHUB_REF_NAME", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/out/reel_{RUN}.mp4"
    caption = (OUT / f"reel_{RUN}.txt").read_text()
    for _ in range(12):  # wait until the pushed file is publicly reachable
        if requests.head(url).status_code == 200:
            break
        time.sleep(10)
    else:
        raise RuntimeError("Video URL not public. Is the repo public?")
    uid, tok = os.environ["IG_USER_ID"], os.environ["IG_ACCESS_TOKEN"]
    r = requests.post(f"{GRAPH}/{uid}/media", data={
        "media_type": "REELS", "video_url": url, "caption": caption,
        "share_to_feed": "true", "access_token": tok})
    r.raise_for_status()
    cid = r.json()["id"]
    for _ in range(40):
        s = requests.get(f"{GRAPH}/{cid}", params={
            "fields": "status_code", "access_token": tok}).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram error: {s}")
        time.sleep(15)
    r = requests.post(f"{GRAPH}/{uid}/media_publish",
                      data={"creation_id": cid, "access_token": tok})
    r.raise_for_status()
    print("Published! Media ID:", r.json()["id"])


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        build(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "publish":
        publish()
    else:
        sys.exit(__doc__)
