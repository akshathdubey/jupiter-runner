from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from supabase import create_client

JOB_ID = os.environ["JOB_ID"]

QUALITY = (
    os.environ.get(
        "QUALITY",
        "normal",
    )
    .strip()
    .lower()
)

TARGET_MINUTES = int(
    os.environ.get(
        "TARGET_MINUTES",
        "5",
    )
)
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("TTS_VOLUME", "+0%")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "jupiter-temp"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def update_job(**values) -> None:
    supabase.table("jobs").update(values).eq("id", JOB_ID).execute()


def get_job() -> dict:
    result = supabase.table("jobs").select("*").eq("id", JOB_ID).single().execute()
    if not result.data:
        raise RuntimeError(f"Job {JOB_ID} was not found.")
    return result.data


def download_object(remote_path: str, local_path: Path) -> None:
    local_path.write_bytes(
        supabase.storage.from_(BUCKET).download(remote_path)
    )


def download_image_assets(
    visual: dict,
    work: Path,
) -> dict:
    """Download durable image assets and resolve renderer references."""
    if not isinstance(visual, dict):
        raise TypeError("Visual artifact must be a dictionary.")

    assets = (
        visual.get("image_assets")
        or (visual.get("visual_system") or {}).get("image_assets")
        or []
    )

    if not isinstance(assets, list):
        assets = []

    asset_dir = work / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)

    resolved = []

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        storage_path = str(
            asset.get("storage_path") or ""
        ).strip()

        asset_id = str(
            asset.get("id") or ""
        ).strip()

        if not storage_path or not asset_id:
            continue

        local_path = (
            asset_dir
            / Path(storage_path).name
        )

        download_object(
            storage_path,
            local_path,
        )

        copy_asset = dict(asset)
        copy_asset["path"] = str(
            local_path.resolve()
        )
        copy_asset["render_path"] = str(
            local_path.resolve()
        )

        resolved.append(copy_asset)

    visual = dict(visual)
    visual["image_assets"] = resolved

    registry = {
        str(item["id"]): item
        for item in resolved
        if item.get("id")
    }

    scenes = []

    for scene in visual.get("scenes", []) or []:
        if not isinstance(scene, dict):
            continue

        scene_copy = dict(scene)
        primary = scene_copy.get(
            "primary_visual"
        )

        if not isinstance(primary, dict):
            scenes.append(scene_copy)
            continue

        primary_copy = dict(primary)

        image_id = str(
            primary_copy.get("image_id") or ""
        ).strip()

        if image_id in registry:
            primary_copy["render_path"] = registry[
                image_id
            ]["path"]

        image_ids = primary_copy.get(
            "image_ids"
        )

        if isinstance(image_ids, list):
            primary_copy["render_assets"] = [
                {
                    "id": str(image_id),
                    "path": registry[
                        str(image_id)
                    ]["path"],
                }
                for image_id in image_ids
                if str(image_id) in registry
            ]

        image = primary_copy.get(
            "image"
        )

        if isinstance(image, dict):
            image_copy = dict(image)

            nested_id = str(
                image_copy.get("id")
                or image_copy.get("image_id")
                or ""
            ).strip()

            if nested_id in registry:
                image_copy["path"] = registry[
                    nested_id
                ]["path"]

                image_copy["render_path"] = registry[
                    nested_id
                ]["path"]

            primary_copy["image"] = image_copy

        scene_copy["primary_visual"] = primary_copy
        scenes.append(scene_copy)

    visual["scenes"] = scenes

    return visual


def upload_video(local_path: Path, remote_path: str) -> None:
    result = supabase.storage.from_(BUCKET).upload(
        remote_path,
        local_path.read_bytes(),
        {
            "content-type": "video/mp4",
            "cache-control": "no-store",
            "upsert": "true",
        },
    )
    if getattr(result, "error", None):
        raise RuntimeError(f"Video upload failed: {result.error}")


def main() -> None:
    job = get_job()

    print("========================================")
    print("       JUPITER PRODUCTION JOB")
    print("========================================")
    print(f"JOB_ID         = {JOB_ID}")
    print(f"DB_SUBJECT     = {job.get('subject')!r}")
    print(f"TARGET_MINUTES = {TARGET_MINUTES}")
    print(f"QUALITY        = {QUALITY}")
    print("========================================")

    job_target_minutes = job.get(
        "target_minutes"
    )

    if (
        job_target_minutes is not None
        and int(job_target_minutes)
        != TARGET_MINUTES
    ):
        raise RuntimeError(
            "Job duration mismatch: "
            f"database={job_target_minutes}, "
            f"workflow={TARGET_MINUTES}"
        )
    teacher_path = job.get("teacher_path")
    visual_path = job.get("visual_path")
    if not teacher_path or not visual_path:
        raise RuntimeError("Job is missing input artifact paths.")

    sys.path.insert(0, str(Path(__file__).resolve().parent / "jupiter-core"))

    from app.intelligence.production_pipeline import (  # type: ignore[import-not-found]
    generate_final_video,
)

    pipeline_quality = "elite" if QUALITY == "premium" else QUALITY

    if pipeline_quality not in {"normal", "elite"}:
        raise RuntimeError(
            f"Invalid quality: {pipeline_quality}"
        )

    with tempfile.TemporaryDirectory(prefix=f"jupiter-{JOB_ID}-") as temp_dir:
        work = Path(temp_dir)
        teacher_file = work / "artifact2.json"
        visual_file = work / "artifact3.json"
        output_file = work / "final.mp4"

        update_job(status="running", stage="downloading_inputs", progress=3)
        download_object(teacher_path, teacher_file)
        download_object(visual_path, visual_file)

        teacher = json.loads(teacher_file.read_text(encoding="utf-8"))
        visual = json.loads(visual_file.read_text(encoding="utf-8"))

        visual = download_image_assets(
            visual,
            work,
        )

        def production_progress(
            stage: str,
            progress: int,
            details: dict | None = None,
        ) -> None:
            values = {
                "status": "running",
                "stage": stage,
                "progress": int(progress),
                "error": None,
            }

            if details:
                if details.get("audio_duration_seconds") is not None:
                    values["audio_duration_seconds"] = details[
                        "audio_duration_seconds"
                    ]

                if details.get("visual_duration_seconds") is not None:
                    values["render_duration_seconds"] = details[
                        "visual_duration_seconds"
                    ]

            update_job(**values)

        update_job(
            stage="production_started",
            progress=5,
            render_attempt=int(job.get("render_attempt") or 0) + 1,
            error=None,
        )

        result = generate_final_video(
            teacher,
            visual,
            output_file,
            quality=pipeline_quality,
            tts_voice=TTS_VOICE,
            tts_rate=TTS_RATE,
            tts_volume=TTS_VOLUME,
            max_repair_cycles=2,
            render_timeout_seconds=1200,
            progress_callback=production_progress,
        )

        if not result.get("passed"):
            message = result.get("message", "Production pipeline failed.")
            update_job(
                status="failed",
                stage=result.get("failed_stage", "production"),
                progress=100,
                error=message,
                completed_at="now()",
            )
            raise RuntimeError(message)

        if not output_file.exists():
            raise RuntimeError(
                "Production reported success but final.mp4 is missing."
            )

        final_duration = result.get(
            "final_duration_seconds"
        )
        audio_duration = result.get(
            "audio_duration_seconds"
        )
        render_duration = result.get(
            "retimed_visual_seconds"
        )

        update_job(
            stage="uploading",
            progress=97,
            render_duration_seconds=render_duration,
            audio_duration_seconds=audio_duration,
            final_video_duration_seconds=final_duration,
        )

        remote_output = f"jobs/{JOB_ID}/final.mp4"
        upload_video(output_file, remote_output)

        update_job(
            status="completed",
            stage="completed",
            progress=100,
            output_path=remote_output,
            render_duration_seconds=render_duration,
            audio_duration_seconds=audio_duration,
            final_video_duration_seconds=final_duration,
            error=None,
        )

        print("JUPITER PRODUCTION = SUCCESS")
        print(f"OUTPUT = {remote_output}")


if __name__ == "__main__":
    main()