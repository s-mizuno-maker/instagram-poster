"""
reel_generator.py
モノ道楽 Shopify商品画像 → リール動画生成 → Cloudinaryアップロード → Supabase登録
"""

import os
import json
import uuid
import requests
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

import cloudinary
import cloudinary.uploader
from supabase import create_client, Client as SupabaseClient
from moviepy.editor import (
    ImageClip, concatenate_videoclips, AudioFileClip
)
from moviepy.video.fx.fadein import fadein
from moviepy.video.fx.fadeout import fadeout
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import anthropic

load_dotenv()

# ── 設定 ──────────────────────────────────────────────────
CLOUDINARY_CLOUD_NAME = os.environ["CLOUDINARY_CLOUD_NAME"]
CLOUDINARY_API_KEY    = os.environ["CLOUDINARY_API_KEY"]
CLOUDINARY_API_SECRET = os.environ["CLOUDINARY_API_SECRET"]
SUPABASE_URL          = os.environ["SUPABASE_URL"]
SUPABASE_KEY          = os.environ["SUPABASE_KEY"]
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
BGM_PATH              = os.environ.get("BGM_PATH", "bgm.mp3")
LOGO_PATH             = os.environ.get("LOGO_PATH", "monodoraku.png")

SHOP_URL     = "https://monodoraku.com"
SEC_PER_IMG  = 2.5
CATCH_SEC    = 3.0
VIDEO_SIZE   = (1080, 1920)

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── フォント取得（細め明朝体）────────────────────────────
def get_mincho_font(size: int):
    font_paths = [
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ]
    for fp in font_paths:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()

# ── ロゴ（黒→白反転）────────────────────────────────────
def load_logo_white(logo_path: str, max_width: int = 700) -> Image.Image:
    logo = Image.open(logo_path).convert("RGBA")
    r, g, b, a = logo.split()
    r = r.point(lambda x: 255 - x)
    g = g.point(lambda x: 255 - x)
    b = b.point(lambda x: 255 - x)
    logo = Image.merge("RGBA", (r, g, b, a))
    ratio = max_width / logo.width
    logo = logo.resize((max_width, int(logo.height * ratio)), Image.LANCZOS)
    return logo

# ── 画像クロップ（上下フィット・左右センタークロップ）────
def crop_to_frame(img: Image.Image, size: tuple) -> Image.Image:
    W, H = size
    img = img.convert("RGB")
    ratio = H / img.height
    new_w = int(img.width * ratio)
    img = img.resize((new_w, H), Image.LANCZOS)
    if new_w > W:
        left = (new_w - W) // 2
        img = img.crop((left, 0, left + W, H))
    else:
        frame = Image.new("RGB", (W, H), (0, 0, 0))
        frame.paste(img, ((W - new_w) // 2, 0))
        img = frame
    return img

# ── キャッチコピー画面 ────────────────────────────────────
def make_catch_frame(catchcopy: str, size: tuple) -> Image.Image:
    W, H = size
    img = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    lines = []
    for line in catchcopy.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) <= 16:
            lines.append(line)
        else:
            for i in range(0, len(line), 16):
                lines.append(line[i:i+16])

    font_size = 72
    font = get_mincho_font(font_size)
    line_h = font_size + 24
    total_h = len(lines) * line_h
    y = (H - total_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (W - tw) // 2
        # シャドウ
        for dx, dy in [(3, 3), (4, 4)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        # 本文（白）
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    return img

# ── ロゴオーバーレイ（最終フレーム）──────────────────────
def make_logo_frame(img: Image.Image, size: tuple) -> Image.Image:
    W, H = size
    result = img.copy().convert("RGBA")
    if Path(LOGO_PATH).exists():
        logo = load_logo_white(LOGO_PATH, max_width=700)
        lx = (W - logo.width) // 2
        ly = H - logo.height - 120
        result.paste(logo, (lx, ly), logo)
    return result.convert("RGB")

# ── キャッチコピー生成（Claude API）──────────────────────
def generate_catchcopy(product: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return product.get("title_en", product["title"])
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""モノ道楽のInstagramリール動画冒頭用キャッチコピーを作成してください。

条件：
- 日本語で2〜3行（1行最大16文字）
- 詩的・感性的・余韻のある表現
- ヴィンテージ・古物の魅力を伝える

商品名：{product['title']}
カテゴリ：{product.get('product_type', '')}

キャッチコピーのみ出力してください。"""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()

# ── 商品取得 ──────────────────────────────────────────────
def get_posted_ids() -> set:
    try:
        res = supabase.table("posted_products").select("product_id").execute()
        return {str(r["product_id"]) for r in res.data}
    except Exception as e:
        print(f"[WARN] posted_ids取得失敗: {e}")
        return set()

def fetch_products(min_images: int = 5) -> list:
    posted_ids = get_posted_ids()
    en_titles = {}
    page = 1
    while True:
        res = requests.get(f"{SHOP_URL}/en/products.json?limit=250&page={page}")
        items = res.json().get("products", [])
        if not items:
            break
        for p in items:
            en_titles[str(p["id"])] = p["title"]
        page += 1

    candidates = []
    page = 1
    while True:
        res = requests.get(f"{SHOP_URL}/products.json?limit=250&page={page}")
        items = res.json().get("products", [])
        if not items:
            break
        for p in items:
            pid = str(p["id"])
            images = [img["src"] for img in p.get("images", [])]
            if pid in posted_ids:
                continue
            if len(images) < min_images:
                continue
            price = ""
            if p.get("variants"):
                price = p["variants"][0].get("price", "")
            candidates.append({
                "id": pid,
                "title": p["title"],
                "title_en": en_titles.get(pid, p["title"]),
                "vendor": p.get("vendor", ""),
                "product_type": p.get("product_type", ""),
                "tags": p.get("tags", ""),
                "handle": p.get("handle", ""),
                "price": price,
                "images": images,
            })
        page += 1

    print(f"[INFO] 対象商品: {len(candidates)}件")
    return candidates

# ── 動画生成 ──────────────────────────────────────────────
def download_image(url: str, dest: Path) -> Path:
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    dest.write_bytes(res.content)
    return dest

def generate_reel(product: dict, selected_image_urls: list, catchcopy: str, tmpdir: Path) -> Path:
    clips = []

    # 1. キャッチコピー画面
    catch_frame = make_catch_frame(catchcopy, VIDEO_SIZE)
    catch_clip = ImageClip(np.array(catch_frame), duration=CATCH_SEC)
    catch_clip = fadein(catch_clip, 0.5).fx(fadeout, 0.5)
    clips.append(catch_clip)

    # 2. 商品画像
    for i, url in enumerate(selected_image_urls):
        img_path = tmpdir / f"img_{i:02d}.jpg"
        download_image(url, img_path)
        pil = Image.open(img_path)
        pil = crop_to_frame(pil, VIDEO_SIZE)
        # 最終枚にロゴ
        if i == len(selected_image_urls) - 1:
            pil = make_logo_frame(pil, VIDEO_SIZE)
        clip = ImageClip(np.array(pil), duration=SEC_PER_IMG)
        clip = fadein(clip, 0.3).fx(fadeout, 0.3)
        clips.append(clip)

    video = concatenate_videoclips(clips, method="compose")

    if Path(BGM_PATH).exists():
        audio = AudioFileClip(BGM_PATH).subclip(0, video.duration)
        audio = audio.audio_fadeout(1.5)
        video = video.set_audio(audio)

    out_path = tmpdir / f"reel_{product['id']}.mp4"
    video.write_videofile(
        str(out_path), fps=30, codec="libx264",
        audio_codec="aac", preset="fast", logger=None,
    )
    video.close()
    return out_path

# ── Cloudinaryアップロード ────────────────────────────────
def upload_to_cloudinary(video_path: Path, product_id: str) -> str:
    result = cloudinary.uploader.upload(
        str(video_path),
        resource_type="video",
        public_id=f"monodoraku/reels/{product_id}",
        overwrite=True,
    )
    url = result["secure_url"]
    print(f"[INFO] Cloudinary: {url}")
    return url

# ── Supabase登録 ──────────────────────────────────────────
def register_to_supabase(product: dict, video_url: str, caption: str = "", scheduled_time: str = None):
    if scheduled_time is None:
        scheduled_time = (datetime.now() + timedelta(hours=1)).isoformat()
    post_id = str(uuid.uuid4())
    supabase.table("scheduled_posts").insert({
        "post_id": post_id,
        "product_id": product["id"],
        "image_urls": json.dumps([video_url]),
        "caption": caption,
        "scheduled_time": scheduled_time,
        "is_posted": False,
        "product_type": product.get("product_type", ""),
        "tags": product.get("tags", ""),
        "handle": product.get("handle", ""),
    }).execute()
    print(f"[INFO] Supabase登録完了: post_id={post_id}")
    return post_id

# ── メイン（CLIテスト用）─────────────────────────────────
def main():
    products = fetch_products(min_images=5)
    if not products:
        print("[INFO] 対象商品なし。")
        return

    product = products[0]
    print(f"\n[START] {product['title']}")

    catchcopy = generate_catchcopy(product)
    print(f"[CATCH]\n{catchcopy}")

    selected = product["images"][:5]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        try:
            video_path = generate_reel(product, selected, catchcopy, tmpdir)
            video_url = upload_to_cloudinary(video_path, product["id"])
            caption = f"{product['title_en']}\n\n#monodoraku #モノ道楽 #vintage #antique"
            register_to_supabase(product, video_url, caption)
            print(f"[DONE] {product['title']}")
        except Exception as e:
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
