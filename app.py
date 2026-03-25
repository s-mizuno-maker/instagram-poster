import os
import json
import requests
from flask import Flask, render_template, jsonify, request
from instagrapi import Client as InstaClient
import anthropic
from PIL import Image
import tempfile
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone, timedelta
JST = timezone(timedelta(hours=9))
from supabase import create_client, Client as SupabaseClient

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def get_posted_ids():
    try:
        response = supabase.table("posted_products").select("product_id").execute()
        return [str(item["product_id"]) for item in response.data]
    except Exception as e:
        print(f"Error fetching posted ids: {e}")
        return []

def save_posted_id(product_id):
    try:
        supabase.table("posted_products").insert({
            "product_id": str(product_id)
        }).execute()
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
            "is_posted": False
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

scheduler = BackgroundScheduler()

def execute_scheduled_post(post_data):
    try:
        image_urls = json.loads(post_data["image_urls"]) if isinstance(post_data["image_urls"], str) else post_data["image_urls"]
        post_to_instagram(image_urls, post_data["caption"])
        mark_as_posted(post_data.get("post_id"), post_data["product_id"])
        print(f"Successfully posted: {post_data['product_id']}")
    except Exception as e:
        print(f"Scheduled post error: {e}")

def restore_scheduled_jobs():
    posts = get_scheduled_posts()
    now = datetime.now()
    for post in posts:
        try:
            run_time = datetime.fromisoformat(post["scheduled_time"])
            if run_time <= now:
                # 過去の予約は即時実行
                execute_scheduled_post(post)
            else:
                scheduler.add_job(
                    execute_scheduled_post,
                    'date',
                    run_date=run_time,
                    args=[post],
                    id=str(post["post_id"])
                )
        except Exception as e:
            print(f"Error restoring job: {e}")

scheduler.start()
try:
    restore_scheduled_jobs()
except Exception as e:
    print(f"Error restoring scheduled jobs on startup: {e}")

def get_products():
    posted_ids = get_posted_ids()
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
                "sku": sku,
                "body_html": p.get("body_html", ""),
                "vendor": p.get("vendor", ""),
                "product_type": p.get("product_type", ""),
                "tags": p.get("tags", ""),
                "images": [img["src"] for img in p.get("images", [])],
                "posted": str(p["id"]) in posted_ids
            })
        page += 1
    return products

def generate_caption(product):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたはモノ道楽というヴィンテージ・古物販売店のInstagram担当者です。
以下の商品情報をもとに、Instagram投稿用のキャプションを日本語で作成してください。

【ルール】
- 商品紹介の形式で書く（入荷情報ではない）
- 起承転結がわかるシンプルな文章
- 価格・在庫状況は書かない
- 売れた後もずっと残る普遍的な文章
- 最後に「プロフィールのリンクからご覧ください」を入れる
- ハッシュタグは文章の後に改行して3〜5個のみ
- 投稿内容と強く関連するものだけを厳選する
- #monodoraku #モノ道楽 を必ず含める
- 汎用的すぎるタグ（#instagood #reels など）は使わない

【商品情報】
商品名：{product['title']}
ブランド：{product['vendor']}
カテゴリ：{product['product_type']}
タグ：{product['tags']}

キャプション本文とハッシュタグのみ出力してください。"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

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
        img = Image.open(tmp.name)
        img = img.convert("RGB")
        img.save(tmp.name)
        image_paths.append(tmp.name)

    if len(image_paths) == 1:
        cl.photo_upload(image_paths[0], caption)
    else:
        cl.album_upload(image_paths, caption)

    for path in image_paths:
        os.unlink(path)

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

    if scheduled_time:
        # スケジュール投稿（既存のまま）
        post_id = str(datetime.now().timestamp())
        post_data = {
            "id": post_id,
            "product_id": product_id,
            "image_urls": image_urls,
            "caption": caption,
            "scheduled_time": scheduled_time
        }
        save_scheduled_post(post_data)
        scheduler.add_job(
            execute_scheduled_post,
            'date',
            run_date=datetime.fromisoformat(scheduled_time),
            args=[post_data],
            id=post_id
        )
        return jsonify({"success": True, "scheduled": True})
    else:
        # 即時投稿：直接実行
        try:
            post_to_instagram(image_urls, caption)
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
        try:
            scheduler.remove_job(post_id)
        except:
            pass
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
