import os
import json
import requests
from flask import Flask, render_template, jsonify, request
from instagrapi import Client
import anthropic
from PIL import Image
import tempfile

app = Flask(__name__)

# 設定
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SHOPIFY_URL = "https://monodoraku.com/products.json?limit=250"
POSTED_FILE = "posted_ids.json"

def get_posted_ids():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return json.load(f)
    return []

def save_posted_id(product_id):
    ids = get_posted_ids()
    ids.append(str(product_id))
    with open(POSTED_FILE, "w") as f:
        json.dump(ids, f)

def get_products():
    response = requests.get(SHOPIFY_URL)
    data = response.json()
    posted_ids = get_posted_ids()
    products = []
    for p in data.get("products", []):
        products.append({
            "id": str(p["id"]),
            "title": p["title"],
            "body_html": p.get("body_html", ""),
            "vendor": p.get("vendor", ""),
            "product_type": p.get("product_type", ""),
            "tags": p.get("tags", ""),
            "images": [img["src"] for img in p.get("images", [])],
            "posted": str(p["id"]) in posted_ids
        })
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
- ハッシュタグは文章の後に改行して10〜15個
- #monodoraku #モノ道楽 を必ず含める

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
    cl = Client()
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
    products = get_products()
    return render_template("index.html", products=products)

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
    
    try:
        post_to_instagram(image_urls, caption)
        save_posted_id(product_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
