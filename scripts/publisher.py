from __future__ import annotations

import base64
import os
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from supabase import create_client

JOB_ID = os.environ["PUBLICATION_JOB_ID"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "jupiter-temp"
META_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def decrypt(value: str) -> str:
    key = base64.b64decode(os.environ["JUPITER_TOKEN_ENCRYPTION_KEY"])
    if len(key) != 32: raise RuntimeError("JUPITER_TOKEN_ENCRYPTION_KEY must decode to 32 bytes.")
    raw_iv, raw_tag, raw_cipher = value.split(".", 2)
    iv = base64.urlsafe_b64decode(raw_iv + "==")
    tag = base64.urlsafe_b64decode(raw_tag + "==")
    cipher = base64.urlsafe_b64decode(raw_cipher + "==")
    return AESGCM(key).decrypt(iv, cipher + tag, None).decode("utf-8")


def get_job():
    row = supabase.table("publication_jobs").select("*").eq("id", JOB_ID).single().execute().data
    if not row: raise RuntimeError("Publication job not found.")
    return row


def get_content(job):
    row = supabase.table("content_jobs").select("*").eq("id", job["content_job_id"]).single().execute().data
    if not row: raise RuntimeError("Content job not found.")
    return row


def get_connection(job):
    row = supabase.table("social_connections").select("*").eq("id", job["social_connection_id"]).single().execute().data
    if not row: raise RuntimeError("Social connection not found.")
    return row


def download_video(path: str) -> bytes:
    data = supabase.storage.from_(BUCKET).download(path)
    if not data: raise RuntimeError("Generated video is empty.")
    return bytes(data)


def youtube_upload(token: str, content: dict, metadata: dict, video_bytes: bytes) -> str:
    title = str(metadata.get("selected_title") or content.get("prompt", "Jupiter Short"))[:100]
    description = str(metadata.get("description") or "Generated with Jupiter.")
    tags = list(metadata.get("secondary_keywords") or [])[:15]
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(video_bytes))}, json={"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "27"}, "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}, timeout=30)
    init.raise_for_status(); upload_url = init.headers.get("Location")
    if not upload_url: raise RuntimeError("YouTube did not return a resumable upload URL.")
    upload = requests.put(upload_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(len(video_bytes))}, data=video_bytes, timeout=20 * 60)
    upload.raise_for_status(); return str(upload.json().get("id"))


def meta_pages(token: str):
    response = requests.get(f"https://graph.facebook.com/{META_VERSION}/me/accounts", params={"fields": "id,name,access_token,instagram_business_account", "access_token": token}, timeout=30)
    response.raise_for_status(); data = response.json().get("data", [])
    if not data: raise RuntimeError("No Facebook Page is available to publish to.")
    return data[0]


def facebook_publish(token: str, content: dict, metadata: dict, video_bytes: bytes) -> str:
    page = meta_pages(token)
    response = requests.post(f"https://graph.facebook.com/{META_VERSION}/{page['id']}/videos", data={"description": str(metadata.get("description") or content.get("prompt", "")), "access_token": page["access_token"]}, files={"source": ("jupiter.mp4", video_bytes, "video/mp4")}, timeout=20 * 60)
    response.raise_for_status(); return str(response.json().get("id"))


def instagram_publish(token: str, content: dict, metadata: dict, video_url: str) -> str:
    page = meta_pages(token); ig = page.get("instagram_business_account")
    if not ig: raise RuntimeError("No Instagram Business account is linked to the selected Facebook Page.")
    caption = str(metadata.get("caption") or metadata.get("description") or content.get("prompt", ""))
    create = requests.post(f"https://graph.facebook.com/{META_VERSION}/{ig['id']}/media", data={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": page["access_token"]}, timeout=60)
    create.raise_for_status(); creation_id = create.json().get("id")
    if not creation_id: raise RuntimeError("Instagram did not return a media container id.")
    import time
    for _ in range(30):
        status = requests.get(f"https://graph.facebook.com/{META_VERSION}/{creation_id}", params={"fields": "status_code", "access_token": page["access_token"]}, timeout=30).json().get("status_code")
        if status == "FINISHED": break
        if status in {"ERROR", "EXPIRED"}: raise RuntimeError(f"Instagram media processing failed: {status}")
        time.sleep(4)
    publish = requests.post(f"https://graph.facebook.com/{META_VERSION}/{ig['id']}/media_publish", data={"creation_id": creation_id, "access_token": page["access_token"]}, timeout=60)
    publish.raise_for_status(); return str(publish.json().get("id"))


def main():
    job = get_job(); content = get_content(job); connection = get_connection(job)
    supabase.table("publication_jobs").update({"status": "uploading", "error": None}).eq("id", JOB_ID).execute()
    token = decrypt(connection["encrypted_access_token"]); video_bytes = download_video(content["output_path"])
    seo = supabase.table("seo_metadata").select("*").eq("content_job_id", content["id"]).single().execute().data or {}
    platform = job["platform"]
    if platform == "youtube": post_id = youtube_upload(token, content, seo, video_bytes)
    elif platform == "facebook": post_id = facebook_publish(token, content, seo, video_bytes)
    elif platform == "instagram":
        signed = supabase.storage.from_(BUCKET).create_signed_url(content["output_path"], 3600)
        video_url = signed.get("signedURL") or signed.get("signedUrl")
        if not video_url: raise RuntimeError("Could not create a temporary video URL for Instagram.")
        post_id = instagram_publish(token, content, seo, video_url)
    else: raise RuntimeError(f"Unsupported platform: {platform}")
    supabase.table("publication_jobs").update({"status": "published", "published_at": datetime.now(timezone.utc).isoformat(), "platform_post_id": post_id}).eq("id", JOB_ID).execute()
    print(f"PUBLISHED {platform} {post_id}")


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        supabase.table("publication_jobs").update({"status": "failed", "error": str(exc)}).eq("id", JOB_ID).execute(); raise
