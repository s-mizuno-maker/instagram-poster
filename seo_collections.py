"""
seo_collections.py
モノ道楽 Shopify コレクション SEO メタ情報 生成・更新ロジック
GraphQL Admin API でSEOフィールドを更新する
"""

import os
import json
import time
import requests
from anthropic import Anthropic

SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_SHOP          = os.environ.get("SHOPIFY_SHOP", "monodoraku")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")

SHOPIFY_BASE = f"https://{SHOPIFY_SHOP}.myshopify.com"
API_VERSION  = "2025-01"

SHOPIFY_CALL_DELAY = 0.6
CLAUDE_CALL_DELAY  = 0.3

SYSTEM_PROMPT = """あなたはECサイトのSEO専門家です。
ブランド「モノ道楽」（ヴィンテージ・アンティーク商品を扱うShopifyストア）の
コレクションページ用SEOメタ情報を日本語で生成してください。

ターゲット: 30〜50代の品質重視の消費者
トーン: 上質・信頼感・発見の喜び

出力はJSON形式のみ。余分なテキスト・マークダウン・コードブロック不要。
必ずこの形式を守ること:
{"title": "...", "description": "..."}"""

_token            = None
_token_expires_at = 0.0


def get_access_token() -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at - 60:
        return _token
    url = f"{SHOPIFY_BASE}/admin/oauth/access_token"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "client_credentials",
            "client_id":     SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    _token            = body.get("access_token")
    expires_in        = body.get("expires_in", 86399)
    _token_expires_at = time.time() + expires_in
    if not _token:
        raise RuntimeError(f"トークン取得失敗: {resp.text}")
    print("✅ Shopify アクセストークン取得完了")
    return _token


def shopify_get(path: str, params: dict = None) -> dict:
    token = get_access_token()
    url   = f"{SHOPIFY_BASE}/admin/api/{API_VERSION}/{path}"
    resp  = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def shopify_graphql(query: str, variables: dict = None) -> dict:
    """GraphQL Admin API を呼び出す"""
    token = get_access_token()
    url   = f"{SHOPIFY_BASE}/admin/api/{API_VERSION}/graphql.json"
    resp  = requests.post(
        url,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQLエラー: {data['errors']}")
    return data


def fetch_all_collections() -> list:
    collections = []
    for col_type in ("custom_collections", "smart_collections"):
        data  = shopify_get(f"{col_type}.json", {"limit": 250})
        batch = data.get(col_type, [])
        for c in batch:
            c["_col_type"] = col_type
        collections.extend(batch)
        time.sleep(SHOPIFY_CALL_DELAY)
    return collections


def fetch_collection_products(collection_id: int, limit: int = 10) -> list:
    try:
        data   = shopify_get(
            f"collections/{collection_id}/products.json",
            {"limit": limit, "fields": "title"},
        )
        titles = [p["title"] for p in data.get("products", []) if p.get("title")]
        time.sleep(SHOPIFY_CALL_DELAY)
        return titles
    except Exception:
        return []


def generate_seo(collection_name: str, product_titles: list) -> dict:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    products_text = ""
    if product_titles:
        products_text = "\n商品例（最大10件）:\n" + "\n".join(f"- {t}" for t in product_titles[:10])
    user_message = f"""コレクション名: {collection_name}
{products_text}

以下の条件でSEOメタ情報を生成してください:
- title: 60文字以内。末尾に「| モノ道楽」を含める。コレクションの魅力・特徴を前半に。
- description: 120〜160文字。商品カテゴリ・特徴・購買訴求を含める。記号の多用は避ける。

JSON形式のみで出力: {{"title": "...", "description": "..."}}"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)
    warnings = []
    if len(result.get("title", "")) > 60:
        warnings.append(f"title {len(result['title'])}文字（60文字超）")
    desc_len = len(result.get("description", ""))
    if not (120 <= desc_len <= 160):
        warnings.append(f"description {desc_len}文字（120〜160推奨）")
    return {
        "title":       result.get("title", ""),
        "description": result.get("description", ""),
        "warnings":    warnings,
    }


def update_collection_seo(collection: dict, seo: dict) -> bool:
    """
    GraphQL collectionUpdate でSEOを更新する
    REST ID → GraphQL GID に変換して使用
    """
    col_id = collection["id"]
    gid    = f"gid://shopify/Collection/{col_id}"

    mutation = """
    mutation collectionUpdate($input: CollectionInput!) {
      collectionUpdate(input: $input) {
        collection {
          id
          seo {
            title
            description
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    variables = {
        "input": {
            "id": gid,
            "seo": {
                "title":       seo["title"],
                "description": seo["description"],
            }
        }
    }

    try:
        result = shopify_graphql(mutation, variables)
        user_errors = result.get("data", {}).get("collectionUpdate", {}).get("userErrors", [])
        if user_errors:
            print(f"GraphQL userErrors (id={col_id}): {user_errors}")
            return False
        time.sleep(SHOPIFY_CALL_DELAY)
        return True
    except Exception as e:
        print(f"GraphQL失敗 (id={col_id}): {e}")
        return False


def run_seo_update(dry_run: bool = False, limit: int = None, target_id: int = None) -> dict:
    collections = fetch_all_collections()

    if target_id:
        collections = [c for c in collections if c["id"] == target_id]
    elif limit:
        collections = collections[:limit]

    results  = []
    failures = []

    for col in collections:
        col_id   = col["id"]
        col_name = col.get("title", f"Collection {col_id}")
        product_titles = fetch_collection_products(col_id)

        try:
            seo = generate_seo(col_name, product_titles)
            time.sleep(CLAUDE_CALL_DELAY)
        except Exception as e:
            failures.append({"id": col_id, "name": col_name, "reason": str(e)})
            continue

        record = {
            "id":        col_id,
            "name":      col_name,
            "type":      col.get("_col_type"),
            "seo_title": seo["title"],
            "seo_desc":  seo["description"],
            "warnings":  seo["warnings"],
            "updated":   False,
        }

        if not dry_run:
            ok = update_collection_seo(col, seo)
            record["updated"] = ok
            if not ok:
                failures.append({"id": col_id, "name": col_name, "reason": "GraphQL失敗"})
        else:
            record["updated"] = "dry_run"

        results.append(record)

    success_count = len([r for r in results if r["updated"] not in (False,)])

    return {
        "summary": {
            "total":    len(collections),
            "success":  success_count,
            "failures": len(failures),
            "dry_run":  dry_run,
        },
        "results":  results,
        "failures": failures,
    }
