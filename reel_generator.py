"""
reel_generator.py
モノ道楽 Shopify商品画像 → リール動画生成 → Cloudinaryアップロード → Supabase登録
"""

import os
import json
import uuid
import requests
import tempfile
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

import cloudinary
import cloudinary.uploader
from supabase import create_client, Client as SupabaseClient
from moviepy.editor import (
    ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip
)
from moviepy.video.fx.fadein import fadein
from moviepy.video.fx.fadeout import fadeout
from PIL import Image, ImageDraw, ImageFont
import numpy as np

load_dotenv()

# ── 設定 ──────────────────────────────────────────────────
CLOUDINARY_CLOUD_NAME = os.environ["CLOUDINARY_CLOUD_NAME"]
CLOUDINARY_API_KEY    = os.environ["CLOUDINARY_API_KEY"]
CLOUDINARY_API_SECRET = os.environ["CLOUDINARY_API_SECRET"]
SUPABASE_URL          = os.environ["SUPABASE_URL"]
SUPABASE_KEY          = os.environ["SUPABASE_KEY"]
BGM_PATH              = os.environ.get("BGM_PATH", "bgm.mp3")

BRAND_NAME   = "monodoraku"
SHOP_URL     = "https://monodoraku.com"
SEC_PER_IMG  = 2.5   # 1枚あたりの秒数
MIN_IMAGES   = 10    # 最低必要枚数
VIDEO_SIZE   = (1080, 1920)  # リール縦動画サイズ

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── 商品取得 ──────────────────────────────────────────────
def get_posted_ids() -> set:
    try:
        res = supabase.table("posted_products").select("product_id").execute()
        return {str(r["product_id"]) for r in res.data}
    except Exception as e:
        print(f"[WARN] posted_ids取得失敗: {e}")
        return set()

def fetch_products_with_images(min_images: int = MIN_IMAGES) -> list:
    """未投稿かつ画像min_images枚以上の商品を返す"""
    posted_ids = get_posted_ids()

    # 英語タイトル取得
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

    print(f"[INFO] 動画生成対象商品: {len(candidates)}件")
    return candidates

# ── 画像処理 ──────────────────────────────────────────────
def download_image(url: str, dest: Path) -> Path:
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    dest.write_bytes(res.content)
    return dest

def fit_image_to_frame(img: Image.Image, size: tuple) -> Image.Image:
    """縦動画フレームにアスペクト比を保ってfit（上下左右黒帯）"""
    w, h = size
    img = img.convert("RGB")
    img.thumbnail((w, h), Image.LANCZOS)
    frame = Image.new("RGB", (w, h), (0, 0, 0))
    offset = ((w - img.width) // 2, (h - img.height) // 2)
    frame.paste(img, offset)
    return frame

def add_text_overlay(img: Image.Image, lines: list[str], position: str = "bottom") -> Image.Image:
    """
    lines: テキスト行リスト
    position: "top" | "bottom"
    """
    draw = ImageDraw.Draw(img.copy())
    img = img.copy()
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # フォント（システムフォントにフォールバック）
    font_paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font_large, font_small = None, None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                from PIL import ImageFont
                font_large = ImageFont.truetype(fp, 52)
                font_small = ImageFont.truetype(fp, 38)
                break
            except Exception:
                continue
    if font_large is None:
        font_large = ImageFont.load_default()
        font_small = font_large

    # 半透明オーバーレイ帯
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    band_h = len(lines) * 70 + 60
    band_y = 40 if position == "top" else H - band_h - 40
    band = Image.new("RGBA", (W, band_h), (0, 0, 0, 160))
    overlay.paste(band, (0, band_y))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    y = band_y + 30
    for i, line in enumerate(lines):
        font = font_large if i == 0 else font_small
        # テキスト幅を計測して中央揃え
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (W - tw) // 2
        # 影
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 200))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += 70

    return img

# ── 動画生成 ──────────────────────────────────────────────
def generate_reel(product: dict, tmpdir: Path) -> Path:
    """動画ファイルを生成してパスを返す"""
    images = product["images"][:10]  # 最大10枚
    clips = []

    for i, url in enumerate(images):
        img_path = tmpdir / f"img_{i:02d}.jpg"
        download_image(url, img_path)
        pil = Image.open(img_path)
        pil = fit_image_to_frame(pil, VIDEO_SIZE)

        # テキストオーバーレイ
        if i == 0:
            # 1枚目：ブランド名
            pil = add_text_overlay(pil, [BRAND_NAME.upper()], position="top")
        elif i == len(images) - 1:
            # 最終枚：商品名・価格
            title_en = product["title_en"]
            # 長い場合は折り返し
            if len(title_en) > 30:
                title_en = textwrap.fill(title_en, width=30)
            price_str = f"¥{int(float(product['price'])):,}" if product.get("price") else ""
            lines = [title_en] + ([price_str] if price_str else [])
            pil = add_text_overlay(pil, lines, position="bottom")

        # numpy配列→ImageClip
        frame = np.array(pil)
        clip = ImageClip(frame, duration=SEC_PER_IMG)
        clip = fadein(clip, 0.3).fx(fadeout, 0.3)
        clips.append(clip)

    video = concatenate_videoclips(clips, method="compose")

    # BGM
    if Path(BGM_PATH).exists():
        audio = AudioFileClip(BGM_PATH).subclip(0, video.duration)
        audio = audio.audio_fadeout(1.5)
        video = video.set_audio(audio)
    else:
        print(f"[WARN] BGMファイルが見つかりません: {BGM_PATH}")

    out_path = tmpdir / f"reel_{product['id']}.mp4"
    video.write_videofile(
        str(out_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        logger=None,
    )
    video.close()
    return out_path

# ── Cloudinaryアップロード ────────────────────────────────
def upload_to_cloudinary(video_path: Path, product_id: str) -> str:
    """動画をアップロードしてURLを返す"""
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
    """scheduled_postsテーブルに登録"""
    if scheduled_time is None:
        # デフォルト：1時間後
        scheduled_time = (datetime.now() + timedelta(hours=1)).isoformat()

    post_id = str(uuid.uuid4())
    supabase.table("scheduled_posts").insert({
        "post_id": post_id,
        "product_id": product["id"],
        "image_urls": json.dumps([video_url]),  # リール動画URLを格納
        "caption": caption,
        "scheduled_time": scheduled_time,
        "is_posted": False,
        "product_type": product.get("product_type", ""),
        "tags": product.get("tags", ""),
        "handle": product.get("handle", ""),
    }).execute()
    print(f"[INFO] Supabase登録完了: post_id={post_id}")
    return post_id

# ── メイン ────────────────────────────────────────────────
def main():
    products = fetch_products_with_images(min_images=MIN_IMAGES)
    if not products:
        print("[INFO] 対象商品なし。終了します。")
        return

    # 最初の1件だけ処理（テスト用）
    # 全件処理する場合は products[:] にする
    target = products[:1]

    for product in target:
        print(f"\n[START] {product['title']} (id={product['id']})")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            try:
                # 動画生成
                video_path = generate_reel(product, tmpdir)
                print(f"[INFO] 動画生成完了: {video_path}")

                # Cloudinaryアップロード
                video_url = upload_to_cloudinary(video_path, product["id"])

                # Supabase登録（キャプションは後で手動入力 or 別途生成）
                caption = f"{product['title_en']}\n\n#monodoraku #モノ道楽 #vintage #antique"
                register_to_supabase(product, video_url, caption)

                print(f"[DONE] {product['title']}")
            except Exception as e:
                import traceback
                print(f"[ERROR] {product['title']}: {e}")
                traceback.print_exc()

if __name__ == "__main__":
    main()
