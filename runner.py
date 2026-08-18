from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from supabase import create_client


JOB_ID = os.environ["JOB_ID"]
QUALITY = os.environ.get("QUALITY", "normal").strip().lower()
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("TTS_VOLUME", "+0%")
TARGET_MINUTES = int(os.environ.get("TARGET_MINUTES", "5"))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "jupiter-temp"

ALLOWED_MINUTES = {3, 5, 10, 15, 30, 60}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def update_job(**values) -> None:
    supabase.table("jobs").update(values).eq("id", JOB_ID).execute()


def get_job() -> dict:
    result = (
        supabase.table("jobs")
        .select("*")
        .eq("id", JOB_ID)
        .single()
        .execute()
    )
    if not result.data:
        raise RuntimeError(f"Job {JOB_ID} was not found.")
    return result.data


def download_object(remote_path: str, local_path: Path) -> None:
    data = supabase.storage.from_(BUCKET).download(remote_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)


def download_image_assets(
    visual: dict,
    work: Path,
) -> dict:
    """
    Download the durable image registry and resolve every selected
    primary_visual reference to a local file.
    """
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

    resolved: list[dict] = []

    for raw in assets:
        if not isinstance(raw, dict):
            continue

        storage_path = str(raw.get("storage_path") or "").strip()
        asset_id = str(raw.get("id") or "").strip()

        if not storage_path or not asset_id:
            continue

        local_path = asset_dir / Path(storage_path).name
        download_object(storage_path, local_path)

        asset = dict(raw)
        asset["path"] = str(local_path.resolve())
        asset["render_path"] = str(local_path.resolve())
        asset["available"] = True
        resolved.append(asset)

    visual = dict(visual)
    visual["image_assets"] = resolved

    registry = {
        str(item["id"]).strip(): item
        for item in resolved
        if item.get("id")
    }

    scenes_out: list[dict] = []

    for raw_scene in visual.get("scenes", []) or []:
        if not isinstance(raw_scene, dict):
            continue

        scene = dict(raw_scene)
        primary = scene.get("primary_visual")

        if not isinstance(primary, dict):
            scenes_out.append(scene)
            continue

        primary = dict(primary)

        # Canonical single image selection.
        image_id = str(primary.get("image_id") or "").strip()

        if image_id:
            asset = registry.get(image_id)
            if asset is None:
                raise RuntimeError(
                    f"Selected image asset {image_id!r} was not downloaded."
                )

            path = str(asset["path"])

            primary["render_path"] = path
            primary["image"] = {
                "id": image_id,
                "caption": str(
                    primary.get("caption")
                    or asset.get("caption")
                    or ""
                ),
                "role": str(
                    primary.get("role")
                    or asset.get("role")
                    or asset.get("educational_role")
                    or ""
                ),
                "fit": str(
                    primary.get("fit")
                    or asset.get("fit")
                    or "contain"
                ),
                "position": str(
                    primary.get("position") or "center"
                ),
            }

        # Existing canonical image reference.
        image = primary.get("image")
        if isinstance(image, dict):
            image_copy = dict(image)
            nested_id = str(
                image_copy.get("id")
                or image_copy.get("image_id")
                or ""
            ).strip()

            if nested_id:
                asset = registry.get(nested_id)
                if asset is None:
                    raise RuntimeError(
                        f"Image reference {nested_id!r} is missing from storage."
                    )
                image_copy["path"] = asset["path"]
                image_copy["render_path"] = asset["path"]
                primary["image"] = image_copy

        # Multiple image references.
        image_ids = primary.get("image_ids")
        if isinstance(image_ids, list):
            render_assets = []

            for raw_id in image_ids[:4]:
                image_id = str(raw_id).strip()
                if not image_id:
                    continue

                asset = registry.get(image_id)
                if asset is None:
                    raise RuntimeError(
                        f"Image asset {image_id!r} is missing from storage."
                    )

                render_assets.append(
                    {
                        "id": image_id,
                        "path": asset["path"],
                    }
                )

            primary["render_assets"] = render_assets

        scene["primary_visual"] = primary
        scenes_out.append(scene)

    visual["scenes"] = scenes_out
    return visual


def upload_video(local_path: Path, remote_path: str) -> None:
    response = (
        supabase.storage
        .from_(BUCKET)
        .upload(
            remote_path,
            local_path.read_bytes(),
            {
                "content-type": "video/mp4",
                "cache-control": "no-store",
                "upsert": "true",
            },
        )
    )
    error = getattr(response, "error", None)
    if error:
        raise RuntimeError(f"Video upload failed: {error}")


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

    if TARGET_MINUTES not in ALLOWED_MINUTES:
        raise RuntimeError(
            "Invalid duration. Allowed values are "
            "3, 5, 10, 15, 30 and 60 minutes."
        )

    db_target = job.get("target_minutes")
    if db_target is not None and int(db_target) != TARGET_MINUTES:
        raise RuntimeError(
            f"Job duration mismatch: database={db_target}, "
            f"workflow={TARGET_MINUTES}"
        )

    teacher_path = job.get("teacher_path")
    visual_path = job.get("visual_path")

    if not teacher_path or not visual_path:
        raise RuntimeError("Job is missing teacher_path or visual_path.")

    runner_root = Path(__file__).resolve().parent
    core_root = runner_root / "jupiter-core"

    if not core_root.exists():
        raise RuntimeError(f"Private jupiter-core not found: {core_root}")

    sys.path.insert(0, str(core_root))

    from app.intelligence.production_pipeline import generate_final_video

    pipeline_quality = "elite" if QUALITY == "premium" else QUALITY
    if pipeline_quality not in {"normal", "elite"}:
        raise RuntimeError(f"Invalid quality: {pipeline_quality}")

    with tempfile.TemporaryDirectory(prefix=f"jupiter-{JOB_ID}-") as temp_dir:
        work = Path(temp_dir)

        teacher_file = work / "artifact2.json"
        visual_file = work / "artifact3.json"
        output_file = work / "final.mp4"

        update_job(
            status="running",
            stage="downloading_inputs",
            progress=3,
            error=None,
        )

        download_object(teacher_path, teacher_file)
        download_object(visual_path, visual_file)

        teacher = json.loads(teacher_file.read_text(encoding="utf-8"))
        visual = json.loads(visual_file.read_text(encoding="utf-8"))

        visual = download_image_assets(visual, work)

        selected_count = 0
        for scene in visual.get("scenes", []) or []:
            primary = scene.get("primary_visual") if isinstance(scene, dict) else None
            if isinstance(primary, dict) and (
                primary.get("image_id")
                or isinstance(primary.get("image"), dict)
                or primary.get("image_ids")
            ):
                selected_count += 1

        print(f"Resolved image assets = {len(visual.get('image_assets', []))}")
        print(f"Scenes with image references = {selected_count}")

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
            message = result.get(
                "message",
                "Production pipeline failed.",
            )
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

        final_duration = result.get("final_duration_seconds")
        audio_duration = result.get("audio_duration_seconds")
        render_duration = result.get("retimed_visual_seconds")

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
