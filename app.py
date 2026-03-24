import os
import json
import requests
from flask import Flask, render_template, jsonify, request
from instagrapi import Client
import anthropic
from PIL import Image
import tempfile
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
# --- 追加: Supabase用ライブラリ ---
from supabase import create_client, Client

app = Flask(__name__)

# --- Supabase接続設定 ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Instagram/Anthropic設定 ---
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# --- データベース操作関数（JSONファイルの代わり） ---

def get_posted_ids():
    """投稿済みIDをSupabaseから取得"""
    try:
        response = supabase.table("scheduled_posts").select("product_id").eq("is_posted", True).execute()
        return [str(item["product_id"]) for item in response.data]
    except Exception as e:
        print(f"Error fetching posted ids: {e}")
        return []

def get_scheduled_posts():
    """未投稿の予約リストをSupabaseから取得"""
    try:
        response = supabase.table("scheduled_posts").select("*").eq("is_posted", False).execute()
        return response.data
    except Exception as e:
        print(f"Error fetching scheduled posts: {e}")
        return []

def save_scheduled_post(post):
    """予約投稿をSupabaseに保存"""
    supabase.table("scheduled_posts").insert({
        "id": post["id"],
        "product_id": post["product_id"],
        "image_urls": post["image_urls"],
        "caption": post["caption"],
        "scheduled_time": post["scheduled_time"],
        "is_posted": False
    }).execute()

def mark_as_posted(post_id, product_id):
    """投稿完了後にフラグを更新"""
    # 予約投稿から実行された場合、その行を完了にする
    if post_id:
        supabase.table("scheduled_posts").update({"is_posted": True}).eq("id", post_id).execute()
    else:
        # 即時投稿の場合、新しいレコードとして「投稿済み」を記録
        supabase.table("scheduled_posts").insert({
            "id": str(datetime.now().timestamp()),
            "product_id": product_id,
            "is_posted": True,
            "caption": "Immediate Post",
            "image_urls": []
        }).execute()

# --- スケジューラ設定 ---
scheduler = BackgroundScheduler()

def execute_scheduled_post(post_data):
    """予約時間に実行される関数"""
    try:
        post_to_instagram(post_data["image_urls"], post_data["caption"])
        # 完了フラグを立てる
        mark_as_posted(post_data["id"], post_data["product_id"])
        print(f"Successfully posted: {post_data['product_id']}")
    except Exception as e:
        print(f"Scheduled post error: {e}")

def restore_scheduled_jobs():
    """【再起動対策】未投稿のジョブをSupabaseから読み込んで再登録"""
    posts = get_scheduled_posts()
    now = datetime.now()
    for post in posts:
        run_time = datetime.fromisoformat(post["scheduled_time"])
        if run_time > now:
            scheduler.add_job(
                execute_scheduled_post,
                'date',
                run_date=run_time,
                args=[post],
                id=str(post["id"])
            )
            print(f"Job restored: {post['product_id']} at {post['scheduled_time']}")

# スケジューラ開始とジョブ復元
scheduler.start()
restore_scheduled_jobs()

# --- 既存のロジック（そのまま） ---

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
- 商品紹介の形式で書く
- 最後に「プロフィールのリンクからご覧ください」を入れる
- #monodoraku #モノ道楽 を必ず含める

【商品情報】
商品名：{product['title']}
ブランド：{product['vendor']}
カテゴリ：{product['product_type']}
タグ：{product['tags']}"""

try:
        message = client.messages.create(
            # モデル名を元のコードで動いていたものに戻します
            model="claude-sonnet-4-20250514", 
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        # 戻り値の取得方法も元に戻します
        return message.content[0].text
    except Exception as e:
        print(f"AI Generation Error: {e}")
        return f"エラーが発生しました: {e}"

def post_to_instagram(image_urls, caption):
    cl = Client()
    # セッション管理（環境変数から読み込み）
    session_data = os.environ.get("INSTAGRAM_SESSION")
    if session_data:
        cl.set_settings(json.loads(session_data))
    
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
        if os.path.exists(path):
            os.unlink(path)

# --- Flask Routes ---

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
        post_id = str(datetime.now().timestamp())
        post_data = {
            "id": post_id,
            "product_id": product_id,
            "image_urls": image_urls,
            "caption": caption,
            "scheduled_time": scheduled_time
        }
        run_time = datetime.fromisoformat(scheduled_time)
        
        # Supabaseに保存
        save_scheduled_post(post_data)
        
        # スケジューラに登録
        scheduler.add_job(
            execute_scheduled_post,
            'date',
            run_date=run_time,
            args=[post_data],
            id=post_id
        )
        return jsonify({"success": True, "scheduled": True})
    else:
        try:
            post_to_instagram(image_urls, caption)
            # 即時投稿をDBに記録
            mark_as_posted(None, product_id)
            return jsonify({"success": True, "scheduled": False})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

@app.route("/api/scheduled")
def api_scheduled():
    return jsonify(get_scheduled_posts())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
