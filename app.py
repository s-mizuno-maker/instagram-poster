import os
import json
import requests
import threading
import time
from flask import Flask, render_template, jsonify, request
from instagrapi import Client as InstaClient
import anthropic
from PIL import Image
import tempfile
from datetime import datetime
from supabase import create_client, Client as SupabaseClient
from seo_collections import run_seo_update

app = Flask(__name__)

SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_SHOP          = os.environ.get("SHOPIFY_SHOP", "monodoraku")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PINTEREST_ACCESS_TOKEN = os.environ.get("PINTEREST_ACCESS_TOKEN", "")

# ── Pinterest ボードマッピング ─────────────────────────────
PINTEREST_BOARD_MAP = {
    "インテリア・家具": "1011339728762128965",
    "キッチン・食器": "1011339728762128967",
    "アクセサリー・小物": "1011339728762128968",
    "アパレル・古着": "1011339728762128969",
}
DEFAULT_BOARD_ID = "1011339728762128965"

# product_type / tags からボードIDを判定
BOARD_KEYWORDS = {
    "1011339728762128965": ["furniture", "interior"],
    "1011339728762128967": ["tableware"],
    "1011339728762128968": ["fashion"],
    "1011339728762128969": ["clothing"],
}

def get_board_id(product_type: str, tags: str) -> str:
    text = (product_type + " " + tags).lower()
    for board_id, keywords in BOARD_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text:
                return board_id
    return DEFAULT_BOARD_ID

def post_to_pinterest(image_url: str, caption: str, product_id: str, product_type: str = "", tags: str = "", handle: str = ""):
    if not PINTEREST_ACCESS_TOKEN:
        print("Pinterest access token not set, skipping.")
        return False
    try:
        board_id = get_board_id(product_type, tags)
        link = f"https://monodoraku.com/products/{handle}" if handle else f"https://monodoraku.com"
        title = caption.split("\n")[0][:100]
        description = caption[:500]
        headers = {
            "Authorization": f"Bearer {PINTEREST_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "board_id": board_id,
            "title": title,
            "description": description,
            "link": link,
            "media_source": {
                "source_type": "image_url",
                "url": image_url
            }
        }
        response = requests.post(
            "https://api.pinterest.com/v5/pins",
            headers=headers,
            json=data
        )
        print(f"Pinterest post: {response.status_code} board={board_id} link={link}")
        return response.status_code == 201
    except Exception as e:
        print(f"Pinterest post error: {e}")
        return False

# ── Supabase helpers ──────────────────────────────────────
def get_posted_ids():
    try:
        response = supabase.table("posted_products").select("product_id").execute()
        return [str(item["product_id"]) for item in response.data]
    except Exception as e:
        print(f"Error fetching posted ids: {e}")
        return []

def save_posted_id(product_id):
    try:
        supabase.table("posted_products").insert({"product_id": str(product_id)}).execute()
    except Exception as e:
        print(f"Error saving posted id: {e}")

def get_scheduled_posts():
    try:
        response = supabase.table("scheduled_posts").select("*").eq("is_posted", False).execute()
        return response.data
    except Exception as e:
        print(f"Error fetching scheduled posts: {e}")
        return []

def save_scheduled_post(post):
    try:
        supabase.table("scheduled_posts").insert({
            "post_id": post["id"],
            "product_id": post["product_id"],
            "image_urls": json.dumps(post["image_urls"]),
            "caption": post["caption"],
            "scheduled_time": post["scheduled_time"],
            "is_posted": False,
            "product_type": post.get("product_type", ""),
            "tags": post.get("tags", ""),
            "handle": post.get("handle", ""),
        }).execute()
    except Exception as e:
        print(f"Error saving scheduled post: {e}")

def mark_as_posted(post_id, product_id):
    try:
        save_posted_id(product_id)
        if post_id:
            supabase.table("scheduled_posts").update({"is_posted": True}).eq("post_id", post_id).execute()
    except Exception as e:
        print(f"Error marking as posted: {e}")

# ── Instagram 投稿 ────────────────────────────────────────
def post_to_instagram(image_urls, caption):
    cl = InstaClient()
    session = os.environ.get("INSTAGRAM_SESSION")
    if session:
        cl.set_settings(json.loads(session))
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    else:
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    image_paths = []
    for url in image_urls:
        response = requests.get(url)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(response.content)
        tmp.close()
        img = Image.open(tmp.name).convert("RGB")
        img.save(tmp.name)
        image_paths.append(tmp.name)
    if len(image_paths) == 1:
        cl.photo_upload(image_paths[0], caption)
    else:
        cl.album_upload(image_paths, caption)
    for path in image_paths:
        os.unlink(path)

# ── スケジュール実行 ──────────────────────────────────────
def execute_scheduled_post(post_data):
    try:
        image_urls = json.loads(post_data["image_urls"]) if isinstance(post_data["image_urls"], str) else post_data["image_urls"]
        post_to_instagram(image_urls, post_data["caption"])
        # Pinterest投稿
        if image_urls:
            post_to_pinterest(
                image_url=image_urls[0],
                caption=post_data["caption"],
                product_id=post_data["product_id"],
                product_type=post_data.get("product_type", ""),
                tags=post_data.get("tags", ""),
                handle=post_data.get("handle", ""),
            )
        mark_as_posted(post_data.get("post_id"), post_data["product_id"])
        print(f"Successfully posted: {post_data['product_id']}")
    except Exception as e:
        print(f"Scheduled post error: {e}")

def check_and_execute_scheduled_posts():
    while True:
        try:
            posts = get_scheduled_posts()
            now = datetime.now()
            for post in posts:
                run_time = datetime.fromisoformat(post["scheduled_time"])
                if run_time <= now:
                    print(f"実行: {post['post_id']}")
                    execute_scheduled_post(post)
        except Exception as e:
            print(f"Polling error: {e}")
        time.sleep(60)

threading.Thread(target=check_and_execute_scheduled_posts, daemon=True).start()

# ── 商品取得 ──────────────────────────────────────────────
def get_products():
    posted_ids = get_posted_ids()

    en_titles = {}
    page = 1
    while True:
        url = f"https://monodoraku.com/en/products.json?limit=250&page={page}"
        response = requests.get(url)
        data = response.json()
        items = data.get("products", [])
        if not items:
            break
        for p in items:
            en_titles[str(p["id"])] = p["title"]
        page += 1

    products = []
    page = 1
    while True:
        url = f"https://monodoraku.com/products.json?limit=250&page={page}"
        response = requests.get(url)
        data = response.json()
        items = data.get("products", [])
        if not items:
            break
        for p in items:
            sku = ""
            if p.get("variants"):
                sku = p["variants"][0].get("sku", "")
            products.append({
                "id": str(p["id"]),
                "title": p["title"],
                "title_en": en_titles.get(str(p["id"]), p["title"]),
                "sku": sku,
                "body_html": p.get("body_html", ""),
                "vendor": p.get("vendor", ""),
                "product_type": p.get("product_type", ""),
                "tags": p.get("tags", ""),
                "handle": p.get("handle", ""),
                "images": [img["src"] for img in p.get("images", [])],
                "posted": str(p["id"]) in posted_ids
            })
        page += 1
    return products

# ── キャプション生成 ──────────────────────────────────────
def generate_caption(product):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたはモノ道楽というヴィンテージ・古物販売店のInstagram担当者です。
以下の商品情報をもとに、Instagram投稿用のキャプションを日本語で作成してください。

【ターゲット】
・30〜50代の男性・女性
・インテリア好きで、安いものよりも良質なものを求めている
・落ち着きがあり静かな時間を過ごすのを好む

【必須条件】
1. 冒頭1行目に「商品名（英語）」をそのまま記載する。翻訳や変換は一切しないこと。
2. 次の行から感性的・詩的なキャッチコピーで始める。（例：『静かな朝に寄り添う一杯を楽しむ』）
3. 商品の特徴や魅力を自然にストーリーへ溶け込ませる。
4. 上品かつ親しみがある文章。
5. 使用シーンやコーディネートを想起させる短い描写を入れる。
6. 後述する【商品情報】内の「商品説明文」から情報を抽出し、参考にして文章生成する。

【ハッシュタグルール】
- 文章の後に改行して3〜5個のみ
- 投稿内容と強く関連するものだけを厳選する
- #monodoraku #モノ道楽 を必ず含める
- 汎用的すぎるタグ（#instagood #reels など）は使わない

【商品情報】
商品名（日本語）：{product['title']}
商品名（英語）：{product.get('title_en', product['title'])}
ブランド：{product['vendor']}
カテゴリ：{product['product_type']}
タグ：{product['tags']}
商品説明文：{product['body_html']}

キャプション本文とハッシュタグのみ出力してください。"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/products")
def api_products():
    return jsonify(get_products())

@app.route("/api/generate_caption", methods=["POST"])
def api_generate_caption():
    product = request.json
    caption = generate_caption(product)
    return jsonify({"caption": caption})

@app.route("/api/post", methods=["POST"])
def api_post():
    data = request.json
    product_id = data["product_id"]
    image_urls = data["image_urls"]
    caption = data["caption"]
    scheduled_time = data.get("scheduled_time")
    product_type = data.get("product_type", "")
    tags = data.get("tags", "")
    handle = data.get("handle", "")

    if scheduled_time:
        post_id = str(datetime.now().timestamp())
        post_data = {
            "id": post_id,
            "product_id": product_id,
            "image_urls": image_urls,
            "caption": caption,
            "scheduled_time": scheduled_time,
            "product_type": product_type,
            "tags": tags,
            "handle": handle,
        }
        save_scheduled_post(post_data)
        return jsonify({"success": True, "scheduled": True})
    else:
        try:
            post_to_instagram(image_urls, caption)
            # Pinterest投稿
            if image_urls:
                post_to_pinterest(
                    image_url=image_urls[0],
                    caption=caption,
                    product_id=product_id,
                    product_type=product_type,
                    tags=tags,
                    handle=handle,
                )
            mark_as_posted(None, product_id)
            return jsonify({"success": True, "scheduled": False})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/scheduled")
def api_scheduled():
    return jsonify(get_scheduled_posts())

@app.route("/api/cancel_scheduled", methods=["POST"])
def api_cancel_scheduled():
    data = request.json
    post_id = data["post_id"]
    try:
        supabase.table("scheduled_posts").delete().eq("post_id", post_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ── SEO関連 ───────────────────────────────────────────────
_seo_job  = {"running": False, "result": None, "error": None}
_seo_lock = threading.Lock()

@app.route("/seo")
def seo_page():
    return render_template("seo.html")

@app.route("/api/seo/run", methods=["POST"])
def seo_run():
    global _seo_job
    with _seo_lock:
        if _seo_job["running"]:
            return jsonify({"error": "既に実行中です"}), 409
        data      = request.get_json(silent=True) or {}
        dry_run   = bool(data.get("dry_run", False))
        limit     = data.get("limit")
        target_id = data.get("target_id")
        _seo_job  = {"running": True, "result": None, "error": None}

    def _worker():
        global _seo_job
        try:
            result = run_seo_update(
                dry_run=dry_run,
                limit=int(limit) if limit else None,
                target_id=int(target_id) if target_id else None,
            )
            with _seo_lock:
                _seo_job["result"]  = result
                _seo_job["running"] = False
        except Exception as e:
            with _seo_lock:
                _seo_job["error"]   = str(e)
                _seo_job["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "started", "dry_run": dry_run}), 202

@app.route("/api/seo/status")
def seo_status():
    with _seo_lock:
        return jsonify(dict(_seo_job))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
