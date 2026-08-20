from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from supabase import create_client

JOB_ID = os.environ["CONTENT_JOB_ID"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "jupiter-temp"
QUALITY = os.environ.get("QUALITY", "normal").strip().lower()
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "akshathdubey")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "jupiter-runner")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
NOW = lambda: datetime.now(timezone.utc).isoformat()


def update_job(**values):
    supabase.table("content_jobs").update(values).eq("id", JOB_ID).execute()


def get_job():
    result = supabase.table("content_jobs").select("*").eq("id", JOB_ID).single().execute()
    if not result.data:
        raise RuntimeError(f"Content job {JOB_ID} not found")
    return result.data


def download(path: str, destination: Path):
    data = supabase.storage.from_(BUCKET).download(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    if destination.stat().st_size <= 0:
        raise RuntimeError(f"Empty source asset: {path}")


def build_source_context(assets: list[dict], work: Path) -> str:
    chunks = []
    for index, asset in enumerate(assets[:20], start=1):
        path = str(asset.get("storage_path") or "")
        name = str(asset.get("original_filename") or f"asset-{index}")
        local = work / "assets" / name.replace("/", "_")
        download(path, local)
        mime = str(asset.get("mime_type") or "")
        if mime.startswith("text/") or mime == "text/csv":
            text = local.read_text(encoding="utf-8", errors="ignore")
            chunks.append(f"ASSET {index}: {name}\n{text[:10000]}")
        elif mime == "application/pdf":
            try:
                import fitz
                doc = fitz.open(local)
                text = "\n".join(page.get_text("text") for page in doc[:8])
                chunks.append(f"ASSET {index}: {name}\nPDF EXTRACT:\n{text[:16000]}")
                doc.close()
            except Exception as exc:
                chunks.append(f"ASSET {index}: {name} (PDF text extraction unavailable: {exc})")
        else:
            chunks.append(f"ASSET {index}: {name} ({mime or 'binary asset'}; inspect visually during rendering)")
    return "\n\n".join(chunks)


def build_artifact(prompt: str, source_context: str) -> dict:
    blocks = [{"id": "prompt_1", "page": 1, "text": prompt, "importance": "core"}]
    if source_context:
        blocks.append({"id": "source_context_1", "page": 1, "text": source_context[:24000], "importance": "core"})
    return {"document": {"title": prompt[:100]}, "content_blocks": blocks, "tables": [], "images": [], "equations": [], "concepts": [{"name": "Requested topic", "description": prompt, "importance": "core", "source_evidence": ["prompt_1"]}]}


def dispatch_publication(publication_job_id: str):
    token = os.environ.get("GITHUB_ACTIONS_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_ACTIONS_TOKEN is required to start publishing.")
    workflow = os.environ.get("GITHUB_PUBLISH_WORKFLOW", "publish.yml")
    response = requests.post(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow}/dispatches",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2026-03-10", "Content-Type": "application/json"},
        json={"ref": "main", "inputs": {"publication_job_id": publication_job_id}},
        timeout=30,
    )
    if response.status_code not in (201, 204):
        raise RuntimeError(f"Publisher dispatch failed: {response.status_code} {response.text}")


def main():
    job = get_job()
    update_job(status="running", stage="ingesting_assets", progress=5, started_at=NOW(), error=None)
    with tempfile.TemporaryDirectory(prefix=f"jupiter-short-{JOB_ID}-") as temp_dir:
        work = Path(temp_dir)
        assets = supabase.table("source_assets").select("*").eq("source_id", job.get("source_id")).order("created_at").execute().data or []
        source_context = build_source_context(assets, work)
        artifact = build_artifact(str(job["prompt"]), source_context)
        sys_path = Path(__file__).resolve().parent.parent / "jupiter-core"
        if not sys_path.exists():
            raise RuntimeError("Canonical jupiter-core checkout is missing")
        import sys
        sys.path.insert(0, str(sys_path))
        from app.intelligence.shorts_generator import build_short_package
        from app.intelligence.visual_designer import create_visual_design
        from app.intelligence.fact_ledger import build_fact_ledger
        from app.intelligence.production_pipeline import generate_final_video
        from app.intelligence.caption_burn_in import burn_captions

        update_job(stage="planning", progress=15)
        target_seconds = 60
        package = build_short_package(prompt=str(job["prompt"]), source_context=source_context, target_seconds=target_seconds, tone=str(job.get("tone") or "infotainment"), quality=QUALITY)
        teacher = {"learning_objective": package.get("hook") or package.get("title", ""), "audience_assumptions": [package.get("audience", "general audience")], "units": package.get("units", [])}
        fact_ledger = build_fact_ledger(artifact)

        update_job(stage="storyboarding", progress=28)
        visual = create_visual_design(teacher, target_minutes=1, quality=QUALITY, fact_ledger=fact_ledger, subject="infotainment", image_assets=[])
        visual["visual_system"] = dict(visual.get("visual_system") or {})
        visual["visual_system"]["aspect_ratio"] = "9:16"
        seo = {"primary_keyword": package.get("primary_keyword"), "secondary_keywords": package.get("secondary_keywords", []), "long_tail_keywords": package.get("long_tail_keywords", []), "title_options": package.get("titles", []), "selected_title": package.get("title"), "description": package.get("description"), "caption": package.get("caption"), "hashtags": package.get("hashtags", []), "platform_metadata": {"youtube": {"title": package.get("title"), "description": package.get("description")}, "instagram": {"caption": package.get("caption")}, "facebook": {"description": package.get("description")}}}
        update_job(status="script_ready", stage="script_ready", progress=40, script=package, storyboard={"teacher": teacher, "visual": visual})
        seo_row = supabase.table("seo_metadata").insert({"user_id": job["user_id"], "content_job_id": JOB_ID, **seo}).select("id").single().execute().data
        update_job(stage="rendering", progress=45, seo_metadata_id=seo_row["id"] if seo_row else None)

        rendered = work / "rendered.mp4"
        final = work / "final.mp4"
        result = generate_final_video(teacher, visual, rendered, quality=QUALITY, tts_voice=str(job.get("narrator") or "en-IN-NeerjaNeural"), tts_rate="+0%", tts_volume="+0%", max_repair_cycles=2, render_timeout_seconds=1200)
        if not result.get("passed"):
            raise RuntimeError(result.get("message", "Short render failed"))
        update_job(status="qa", stage="captioning", progress=88)
        burn_captions(rendered, teacher, final)
        remote = f"jobs/{JOB_ID}/final.mp4"
        response = supabase.storage.from_(BUCKET).upload(remote, final.read_bytes(), {"content-type": "video/mp4", "cache-control": "no-store", "upsert": "true"})
        if getattr(response, "error", None):
            raise RuntimeError(f"Final video upload failed: {response.error}")

        publication_ids: list[str] = []
        for platform in job.get("platforms") or []:
            connection = supabase.table("social_connections").select("id").eq("user_id", job["user_id"]).eq("platform", platform).eq("status", "active").limit(1).maybe_single().execute().data
            if not connection:
                raise RuntimeError(f"Active {platform} connection disappeared before publishing.")
            existing = supabase.table("publication_jobs").select("id,status").eq("content_job_id", JOB_ID).eq("social_connection_id", connection["id"]).maybe_single().execute().data
            if existing:
                publication_ids.append(str(existing["id"]))
                continue
            created = supabase.table("publication_jobs").insert({"user_id": job["user_id"], "content_job_id": JOB_ID, "social_connection_id": connection["id"], "platform": platform, "status": "queued", "payload": {}}).select("id").single().execute().data
            if created:
                publication_ids.append(str(created["id"]))

        update_job(status="publishing", stage="publishing", progress=98, output_path=remote, completed_at=None, error=None)
        for publication_id in publication_ids:
            dispatch_publication(publication_id)
        update_job(status="completed", stage="published_or_queued", progress=100, output_path=remote, completed_at=NOW(), error=None)
        print("JUPITER SHORT = SUCCESS")
        print(remote)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        update_job(status="failed", stage="error", progress=100, error=str(exc))
        raise
