from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from supabase import create_client


JOB_ID = os.environ["JOB_ID"]
QUALITY = os.environ.get("QUALITY", "normal").strip().lower()
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("TTS_VOLUME", "+0%")
try:
    TARGET_MINUTES = int(os.environ.get("TARGET_MINUTES", "5"))
except ValueError as exc:
    raise RuntimeError("TARGET_MINUTES must be an integer.") from exc

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

BUCKET = "jupiter-temp"

ALLOWED_MINUTES = {3, 5, 10, 15, 30, 60}

if TARGET_MINUTES not in ALLOWED_MINUTES:
    raise RuntimeError(
        f"Unsupported TARGET_MINUTES={TARGET_MINUTES}. "
        f"Allowed values: {sorted(ALLOWED_MINUTES)}"
    )

if QUALITY not in {"normal", "elite"}:
    raise RuntimeError(
        f"Unsupported QUALITY={QUALITY!r}. "
        "Allowed values: normal, elite."
    )

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


def update_job(**values) -> None:
    supabase.table("jobs").update(values).eq(
        "id",
        JOB_ID,
    ).execute()


def get_job() -> dict:
    result = (
        supabase
        .table("jobs")
        .select("*")
        .eq("id", JOB_ID)
        .single()
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            f"Job {JOB_ID} was not found."
        )

    return result.data


def download_object(
    remote_path: str,
    local_path: Path,
) -> None:

    if not remote_path:
        raise RuntimeError(
            "Cannot download an empty storage path."
        )

    print(
        f"Downloading storage object: {remote_path}"
    )

    data = (
        supabase
        .storage
        .from_(BUCKET)
        .download(remote_path)
    )

    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp_path = local_path.with_suffix(
        local_path.suffix + ".part"
    )

    tmp_path.write_bytes(data)

    if not tmp_path.exists():
        raise RuntimeError(
            f"Downloaded object was not created: {local_path}"
        )

    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Downloaded object is empty: {remote_path}"
        )

    tmp_path.replace(local_path)



def _normalize_image_for_manim(
    local_path: Path,
) -> Path:
    """
    Validate the downloaded image and normalize it to PNG when necessary.

    Manim receives a local PNG/JPEG/WebP in the source registry. Converting
    selected assets to PNG avoids renderer failures caused by image codecs
    or unusual source formats while preserving the actual selected image.
    """
    try:
        from PIL import Image
    except Exception:
        # Pillow is normally available through Manim. If it is not, leave
        # the original file untouched and let Manim report the codec error.
        return local_path

    try:
        with Image.open(local_path) as image:
            image.load()

            if image.width <= 0 or image.height <= 0:
                raise RuntimeError(
                    f"Image has invalid dimensions: {local_path}"
                )

            # Keep PNG/JPEG as-is; normalize other formats to PNG.
            if local_path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                return local_path

            normalized = (
                local_path.with_suffix(".png")
            )

            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA")

            image.save(
                normalized,
                format="PNG",
            )

        if not normalized.exists() or normalized.stat().st_size <= 0:
            raise RuntimeError(
                f"PNG normalization produced no usable file: {normalized}"
            )

        # The original non-PNG file is no longer needed by the renderer.
        try:
            local_path.unlink()
        except OSError:
            pass

        return normalized

    except Exception as exc:
        raise RuntimeError(
            f"Downloaded image is not a valid renderable image: "
            f"{local_path}: {exc}"
        ) from exc


def download_image_assets(
    visual: dict,
    work: Path,
) -> dict:

    if not isinstance(visual, dict):
        raise TypeError(
            "Visual artifact must be a dictionary."
        )

    assets = (
        visual.get("image_assets")
        or (
            visual.get("visual_system")
            or {}
        ).get("image_assets")
        or []
    )

    if not isinstance(assets, list):
        assets = []

    asset_dir = work / "assets"

    asset_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    resolved: list[dict] = []

    print(
        f"Image registry entries = {len(assets)}"
    )

    for raw in assets:

        if not isinstance(raw, dict):
            continue

        storage_path = str(
            raw.get("storage_path")
            or ""
        ).strip()

        asset_id = str(
            raw.get("id")
            or ""
        ).strip()

        if not storage_path:
            print(
                "Skipping image without storage_path"
            )
            continue

        if not asset_id:
            print(
                f"Skipping image without id: {storage_path}"
            )
            continue

        filename = Path(
            storage_path
        ).name

        local_path = (
            asset_dir / filename
        )

        print(
            f"Resolving image {asset_id}: "
            f"{storage_path}"
        )

        download_object(
            storage_path,
            local_path,
        )

        local_path = _normalize_image_for_manim(
            local_path
        )

        asset = dict(raw)

        asset["path"] = str(
            local_path.resolve()
        )

        asset["render_path"] = str(
            local_path.resolve()
        )

        asset["available"] = True

        resolved.append(asset)

        print(
            f"Image resolved: {asset_id} "
            f"-> {asset['render_path']}"
        )

    visual = dict(visual)

    visual["image_assets"] = resolved

    registry = {
        str(item["id"]).strip(): item
        for item in resolved
        if item.get("id")
    }

    scenes_out: list[dict] = []

    for raw_scene in (
        visual.get("scenes", [])
        or []
    ):

        if not isinstance(
            raw_scene,
            dict,
        ):
            continue

        scene = dict(
            raw_scene
        )

        primary = scene.get(
            "primary_visual"
        )

        if not isinstance(
            primary,
            dict,
        ):
            scenes_out.append(scene)
            continue

        primary = dict(
            primary
        )

        nested_existing_image = primary.get("image")

        image_id = str(
            primary.get("image_id")
            or (
                nested_existing_image.get("id")
                if isinstance(nested_existing_image, dict)
                else ""
            )
            or (
                nested_existing_image.get("image_id")
                if isinstance(nested_existing_image, dict)
                else ""
            )
            or ""
        ).strip()

        if image_id:

            asset = registry.get(
                image_id
            )

            if asset is None:
                raise RuntimeError(
                    "Selected image asset "
                    f"{image_id!r} was not downloaded."
                )

            path = str(
                asset["path"]
            )

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
                    or asset.get(
                        "educational_role"
                    )
                    or ""
                ),
                "fit": str(
                    primary.get("fit")
                    or asset.get("fit")
                    or "contain"
                ),
                "position": str(
                    primary.get(
                        "position"
                    )
                    or "center"
                ),
                "path": path,
                "render_path": path,
            }

        image = primary.get(
            "image"
        )

        if isinstance(
            image,
            dict,
        ):

            image_copy = dict(
                image
            )

            nested_id = str(
                image_copy.get("id")
                or image_copy.get(
                    "image_id"
                )
                or ""
            ).strip()

            if nested_id:

                asset = registry.get(
                    nested_id
                )

                if asset is None:
                    raise RuntimeError(
                        "Image reference "
                        f"{nested_id!r} is missing "
                        "from storage."
                    )

                image_copy["path"] = (
                    asset["path"]
                )

                image_copy[
                    "render_path"
                ] = asset["path"]

                primary["image"] = (
                    image_copy
                )

        image_ids = primary.get(
            "image_ids"
        )

        if isinstance(
            image_ids,
            list,
        ):

            render_assets = []

            for raw_id in image_ids[:4]:

                image_id = str(
                    raw_id
                ).strip()

                if not image_id:
                    continue

                asset = registry.get(
                    image_id
                )

                if asset is None:
                    raise RuntimeError(
                        "Image asset "
                        f"{image_id!r} is missing "
                        "from storage."
                    )

                render_assets.append(
                    {
                        "id": image_id,
                        "path": asset[
                            "path"
                        ],
                        "render_path": asset[
                            "path"
                        ],
                    }
                )

            primary[
                "render_assets"
            ] = render_assets

        scene[
            "primary_visual"
        ] = primary

        scenes_out.append(
            scene
        )

    visual["scenes"] = (
        scenes_out
    )

    # ------------------------------------------------------------
    # HARD IMAGE INTEGRITY CHECK
    # ------------------------------------------------------------

    print(
        "----------------------------------------"
    )
    print(
        "IMAGE RESOLUTION CHECK"
    )
    print(
        "----------------------------------------"
    )

    for asset in resolved:

        path = Path(
            asset["path"]
        )

        print(
            f"{asset.get('id')} -> {path}"
        )

        if not path.exists():
            raise RuntimeError(
                f"Resolved image does not exist: {path}"
            )

        if path.stat().st_size == 0:
            raise RuntimeError(
                f"Resolved image is empty: {path}"
            )

    print(
        f"Resolved image assets = {len(resolved)}"
    )

    return visual


def upload_video(
    local_path: Path,
    remote_path: str,
) -> None:

    if not local_path.exists():
        raise RuntimeError(
            f"Cannot upload missing video: {local_path}"
        )

    response = (
        supabase
        .storage
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

    error = getattr(
        response,
        "error",
        None,
    )

    if error:
        raise RuntimeError(
            f"Video upload failed: {error}"
        )


def upload_debug_file(
    local_path: Path,
    remote_path: str,
) -> None:

    if not local_path.exists():
        return

    try:

        (
            supabase
            .storage
            .from_(BUCKET)
            .upload(
                remote_path,
                local_path.read_bytes(),
                {
                    "content-type": "text/plain",
                    "cache-control": "no-store",
                    "upsert": "true",
                },
            )
        )

        print(
            f"Debug artifact uploaded: {remote_path}"
        )

    except Exception as exc:

        print(
            "WARNING: Could not upload debug "
            f"artifact: {exc}"
        )


def main() -> None:

    job = get_job()

    print(
        "========================================"
    )
    print(
        "       JUPITER PRODUCTION JOB"
    )
    print(
        "========================================"
    )

    print(
        f"JOB_ID         = {JOB_ID}"
    )

    print(
        f"DB_SUBJECT     = {job.get('subject')!r}"
    )

    print(
        f"TARGET_MINUTES = {TARGET_MINUTES}"
    )

    print(
        f"QUALITY        = {QUALITY}"
    )

    print(
        "========================================"
    )

    if TARGET_MINUTES not in ALLOWED_MINUTES:
        raise RuntimeError(
            "Invalid duration. Allowed values are "
            "3, 5, 10, 15, 30 and 60 minutes."
        )

    db_target = job.get(
        "target_minutes"
    )

    if (
        db_target is not None
        and int(db_target)
        != TARGET_MINUTES
    ):
        raise RuntimeError(
            "Job duration mismatch: "
            f"database={db_target}, "
            f"workflow={TARGET_MINUTES}"
        )

    teacher_path = job.get(
        "teacher_path"
    )

    visual_path = job.get(
        "visual_path"
    )

    if not teacher_path:
        raise RuntimeError(
            "Job is missing teacher_path."
        )

    if not visual_path:
        raise RuntimeError(
            "Job is missing visual_path."
        )

    runner_root = (
        Path(__file__).resolve().parent
    )

    core_root = (
        runner_root / "jupiter-core"
    )

    if not core_root.exists():
        raise RuntimeError(
            f"Private jupiter-core not found: {core_root}"
        )

    sys.path.insert(
        0,
        str(core_root),
    )

    from app.intelligence.production_pipeline import (
        generate_final_video,
    )

    pipeline_quality = (
        "elite"
        if QUALITY == "premium"
        else QUALITY
    )

    if pipeline_quality not in {
        "normal",
        "elite",
    }:
        raise RuntimeError(
            f"Invalid quality: {pipeline_quality}"
        )

    # ------------------------------------------------------------
    # Keep failed workspaces locally long enough to upload them.
    # ------------------------------------------------------------

    debug_root = (
        Path.cwd()
        / ".jupiter_debug"
        / JOB_ID
    )

    debug_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.TemporaryDirectory(
        prefix=f"jupiter-{JOB_ID}-"
    ) as temp_dir:

        work = Path(
            temp_dir
        )

        teacher_file = (
            work / "artifact2.json"
        )

        visual_file = (
            work / "artifact3.json"
        )

        output_file = (
            work / "final.mp4"
        )

        try:

            update_job(
                status="running",
                stage="downloading_inputs",
                progress=3,
                error=None,
            )

            download_object(
                teacher_path,
                teacher_file,
            )

            download_object(
                visual_path,
                visual_file,
            )

            teacher = json.loads(
                teacher_file.read_text(
                    encoding="utf-8"
                )
            )

            visual = json.loads(
                visual_file.read_text(
                    encoding="utf-8"
                )
            )

            # ----------------------------------------------------
            # Resolve image assets BEFORE production.
            # ----------------------------------------------------

            visual = download_image_assets(
                visual,
                work,
            )

            # ----------------------------------------------------
            # Save exact production inputs.
            # ----------------------------------------------------

            (
                work
                / "resolved_visual.json"
            ).write_text(
                json.dumps(
                    visual,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            (
                work
                / "teacher.json"
            ).write_text(
                json.dumps(
                    teacher,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            selected_count = 0

            for scene in (
                visual.get(
                    "scenes",
                    [],
                )
                or []
            ):

                if not isinstance(
                    scene,
                    dict,
                ):
                    continue

                primary = scene.get(
                    "primary_visual"
                )

                if not isinstance(
                    primary,
                    dict,
                ):
                    continue

                if (
                    primary.get(
                        "image_id"
                    )
                    or isinstance(
                        primary.get(
                            "image"
                        ),
                        dict,
                    )
                    or primary.get(
                        "image_ids"
                    )
                ):
                    selected_count += 1

            print(
                f"Resolved image assets = "
                f"{len(visual.get('image_assets', []))}"
            )

            print(
                f"Scenes with image references = "
                f"{selected_count}"
            )

            def production_progress(
                stage: str,
                progress: int,
                details: dict | None = None,
            ) -> None:

                values = {
                    "status": "running",
                    "stage": stage,
                    "progress": int(
                        progress
                    ),
                    "error": None,
                }

                if details:

                    if (
                        details.get(
                            "audio_duration_seconds"
                        )
                        is not None
                    ):
                        values[
                            "audio_duration_seconds"
                        ] = details[
                            "audio_duration_seconds"
                        ]

                    if (
                        details.get(
                            "visual_duration_seconds"
                        )
                        is not None
                    ):
                        values[
                            "render_duration_seconds"
                        ] = details[
                            "visual_duration_seconds"
                        ]

                update_job(
                    **values
                )

            update_job(
                stage="production_started",
                progress=5,
                render_attempt=(
                    int(
                        job.get(
                            "render_attempt"
                        )
                        or 0
                    )
                    + 1
                ),
                error=None,
            )

            print(
                "Starting production pipeline..."
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

            # ----------------------------------------------------
            # CRITICAL:
            # Never hide the production result.
            # ----------------------------------------------------

            print(
                "========================================"
            )

            print(
                "PRODUCTION PIPELINE RESULT"
            )

            print(
                json.dumps(
                    result,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
            )

            print(
                "========================================"
            )

            if not result.get(
                "passed"
            ):

                message = result.get(
                    "message",
                    "Production pipeline failed.",
                )

                failed_stage = result.get(
                    "failed_stage",
                    "production",
                )

                # Save exact pipeline response.
                (
                    work
                    / "production_result.json"
                ).write_text(
                    json.dumps(
                        result,
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    ),
                    encoding="utf-8",
                )

                # Save all generated source files.
                source_dump = (
                    work
                    / "generated_sources"
                )

                source_dump.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                # Copy the complete workspace before
                # TemporaryDirectory deletes it.
                if work.exists():

                    shutil.copytree(
                        work,
                        debug_root,
                        dirs_exist_ok=True,
                    )

                update_job(
                    status="failed",
                    stage=failed_stage,
                    progress=100,
                    error=message,
                    completed_at="now()",
                )

                raise RuntimeError(
                    message
                )

            if not output_file.exists():

                raise RuntimeError(
                    "Production reported success "
                    "but final.mp4 is missing."
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
                render_duration_seconds=(
                    render_duration
                ),
                audio_duration_seconds=(
                    audio_duration
                ),
                final_video_duration_seconds=(
                    final_duration
                ),
            )

            remote_output = (
                f"jobs/{JOB_ID}/final.mp4"
            )

            upload_video(
                output_file,
                remote_output,
            )

            # Preserve a local copy for the GitHub artifact/debug bundle.
            # The production workspace is temporary and is deleted at exit.
            debug_output = debug_root / "final.mp4"
            shutil.copy2(output_file, debug_output)

            update_job(
                status="completed",
                stage="completed",
                progress=100,
                output_path=remote_output,
                render_duration_seconds=(
                    render_duration
                ),
                audio_duration_seconds=(
                    audio_duration
                ),
                final_video_duration_seconds=(
                    final_duration
                ),
                error=None,
            )

            print(
                "========================================"
            )

            print(
                "JUPITER PRODUCTION = SUCCESS"
            )

            print(
                f"OUTPUT = {remote_output}"
            )

            print(
                "========================================"
            )

        except Exception as exc:

            # ----------------------------------------------------
            # Preserve the complete workspace for diagnosis.
            # ----------------------------------------------------

            print(
                "========================================"
            )

            print(
                "JUPITER PRODUCTION EXCEPTION"
            )

            print(
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            print(
                "========================================"
            )

            try:

                if work.exists():

                    shutil.copytree(
                        work,
                        debug_root,
                        dirs_exist_ok=True,
                    )

                    print(
                        f"Debug workspace preserved at: "
                        f"{debug_root}"
                    )

            except Exception as copy_exc:

                print(
                    "WARNING: Could not preserve "
                    f"debug workspace: {copy_exc}"
                )

            raise


if __name__ == "__main__":
    main()