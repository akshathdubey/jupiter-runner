from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.shorts_runner import (
    BUCKET,
    JOB_ID,
    QUALITY,
    build_artifact,
    build_source_context,
    get_job,
    supabase,
    update_job,
)

PREVIEW_SECONDS = 10


def _teacher_duration_seconds(teacher: dict) -> int:
    durations = []
    for unit in teacher.get("units", []) or []:
        if isinstance(unit, dict):
            try:
                value = int(round(float(unit.get("duration_seconds", 0))))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                durations.append(value)
    if not durations:
        raise RuntimeError("Teacher plan has no positive duration units for preview rendering.")
    return sum(durations)


def watermark_video(input_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for preview watermarking.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "drawtext=fontcolor=white@0.42:fontsize=34:box=1:boxcolor=000000@0.20:boxborderw=18:"
        "x=(w-text_w)/2:y=h*0.54:text='JUPITER PREVIEW'"
    )
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            str(PREVIEW_SECONDS),
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=10 * 60,
    )
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError((result.stderr or result.stdout or "Preview watermark failed")[-4000:])


def main() -> None:
    job = get_job()
    update_job(
        status="running",
        stage="preview_ingesting",
        progress=5,
        started_at=datetime.now(timezone.utc).isoformat(),
        error=None,
        preview_status="running",
    )
    with tempfile.TemporaryDirectory(prefix=f"jupiter-preview-{JOB_ID}-") as tmp:
        work = Path(tmp)
        assets = (
            supabase.table("source_assets")
            .select("*")
            .eq("source_id", job.get("source_id"))
            .order("created_at")
            .execute()
            .data
            or []
        )
        source_context = build_source_context(assets, work)
        artifact = build_artifact(str(job["prompt"]), source_context)

        core_root = Path(__file__).resolve().parent.parent / "jupiter-core"
        if not core_root.exists():
            raise RuntimeError("Canonical jupiter-core checkout is missing")

        import sys

        sys.path.insert(0, str(core_root))
        from app.intelligence.caption_burn_in import burn_captions
        from app.intelligence.fact_ledger import build_fact_ledger
        from app.intelligence.production_pipeline import generate_final_video
        from app.intelligence.shorts_generator import build_short_package
        from app.intelligence.visual_designer import create_visual_design

        update_job(stage="preview_planning", progress=20)
        package = build_short_package(
            prompt=str(job["prompt"]),
            source_context=source_context,
            target_seconds=PREVIEW_SECONDS,
            tone=str(job.get("tone") or "infotainment"),
            quality=QUALITY,
        )
        teacher = {
            "learning_objective": package.get("hook") or package.get("title", ""),
            "audience_assumptions": [package.get("audience", "general audience")],
            "units": package.get("units", []),
        }
        teacher_duration_seconds = _teacher_duration_seconds(teacher)
        fact_ledger = build_fact_ledger(artifact)

        update_job(stage="preview_storyboarding", progress=35)
        visual = create_visual_design(
            teacher,
            target_minutes=max(1, (teacher_duration_seconds + 59) // 60),
            target_seconds=teacher_duration_seconds,
            quality=QUALITY,
            fact_ledger=fact_ledger,
            subject="infotainment",
            image_assets=[],
        )
        visual["visual_system"] = dict(visual.get("visual_system") or {})
        visual["visual_system"]["aspect_ratio"] = "9:16"
        update_job(
            stage="preview_rendering",
            progress=50,
            script=package,
            storyboard={"teacher": teacher, "visual": visual},
        )

        rendered = work / "preview_render.mp4"
        captioned = work / "preview_captioned.mp4"
        final = work / "preview.mp4"
        result = generate_final_video(
            teacher,
            visual,
            rendered,
            quality="normal",
            tts_voice=str(job.get("narrator") or "en-IN-NeerjaNeural"),
            tts_rate="+0%",
            tts_volume="+0%",
            max_repair_cycles=1,
            render_timeout_seconds=600,
        )
        if not result.get("passed"):
            raise RuntimeError(result.get("message", "Preview render failed"))

        update_job(stage="preview_captioning", progress=75)
        burn_captions(rendered, teacher, captioned, visual=visual)
        watermark_video(captioned, final)

        remote = f"jobs/{JOB_ID}/preview.mp4"
        upload = supabase.storage.from_(BUCKET).upload(
            remote,
            final.read_bytes(),
            {
                "content-type": "video/mp4",
                "cache-control": "no-store",
                "upsert": "true",
            },
        )
        if getattr(upload, "error", None):
            raise RuntimeError(f"Preview upload failed: {upload.error}")

        update_job(
            status="running",
            stage="preview_ready",
            progress=100,
            preview_status="ready",
            preview_path=remote,
            preview_generated_at=datetime.now(timezone.utc).isoformat(),
            preview_watermarked=True,
            error=None,
        )
        print("JUPITER PREVIEW = SUCCESS")
        print(remote)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            update_job(
                status="failed",
                stage="preview_error",
                progress=100,
                preview_status="failed",
                error=str(exc),
            )
        finally:
            raise
