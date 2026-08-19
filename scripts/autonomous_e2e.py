from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from supabase import create_client


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "vit-e2e.pdf.gz.b64"
BUCKET = "jupiter-temp"
OWNER = os.environ.get("GITHUB_OWNER", "akshathdubey")
REPO = os.environ.get("GITHUB_REPO", "jupiter-runner")
ANALYZE_WORKFLOW = "analyze.yml"
RENDER_WORKFLOW = "render.yml"
REF = "main"
TARGET_MINUTES = 3
QUALITY = "normal"
TTS_VOICE = "en-IN-NeerjaNeural"

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def github_dispatch(workflow: str, inputs: dict[str, str]) -> None:
    url = (
        f"https://api.github.com/repos/{OWNER}/{REPO}"
        f"/actions/workflows/{workflow}/dispatches"
    )
    payload = json.dumps({"ref": REF, "inputs": inputs}).encode("utf-8")
    request = urllib.request.Request(
        url,
        method="POST",
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 201, 204):
            raise RuntimeError(
                f"GitHub dispatch failed for {workflow}: HTTP {response.status}"
            )


def get_job(job_id: str) -> dict:
    result = (
        supabase.table("jobs")
        .select("*")
        .eq("id", job_id)
        .single()
        .execute()
    )
    if not result.data:
        raise RuntimeError(f"Job {job_id} disappeared from Supabase.")
    return result.data


def update_job(job_id: str, **values) -> None:
    result = (
        supabase.table("jobs")
        .update(values)
        .eq("id", job_id)
        .execute()
    )
    if getattr(result, "error", None):
        raise RuntimeError(str(result.error))


def wait_for_job(job_id: str, timeout_seconds: int, label: str) -> dict:
    deadline = time.time() + timeout_seconds
    last_stage = None
    while time.time() < deadline:
        job = get_job(job_id)
        status = str(job.get("status") or "").lower()
        stage = str(job.get("stage") or "").lower()
        progress = job.get("progress")

        if stage != last_stage:
            print(f"[{label}] status={status} stage={stage} progress={progress}")
            last_stage = stage

        if status == "completed":
            return job
        if status == "failed":
            raise RuntimeError(
                f"{label} job failed: stage={stage} error={job.get('error')}"
            )

        time.sleep(20)

    raise TimeoutError(f"{label} job {job_id} did not finish before timeout.")


def ensure_bucket() -> None:
    existing = supabase.storage.get_bucket(BUCKET)
    if not existing.error and existing.data:
        return
    created = supabase.storage.create_bucket(
        BUCKET,
        {"public": False, "file_size_limit": 52428800},
    )
    if created.error and "already exists" not in str(created.error).lower():
        raise RuntimeError(f"Could not initialize {BUCKET}: {created.error}")


def load_fixture() -> bytes:
    if not FIXTURE.exists():
        raise RuntimeError(f"Missing E2E fixture: {FIXTURE}")
    packed = base64.b64decode(FIXTURE.read_text(encoding="ascii"))
    data = gzip.decompress(packed)
    digest = hashlib.sha256(data).hexdigest()
    print(f"E2E fixture bytes={len(data)} sha256={digest}")
    if len(data) < 50_000:
        raise RuntimeError("E2E fixture is unexpectedly small.")
    return data


def download_json(path: str) -> dict:
    data = supabase.storage.from_(BUCKET).download(path)
    return json.loads(data.decode("utf-8"))


def create_analysis_job(source: bytes) -> str:
    job_id = str(uuid.uuid4())
    document_path = f"e2e/{job_id}/source-vit-e2e.pdf"

    created = (
        supabase.table("jobs")
        .insert(
            {
                "id": job_id,
                "kind": "analysis",
                "status": "queued",
                "stage": "creating",
                "progress": 1,
                "quality": QUALITY,
                "target_minutes": TARGET_MINUTES,
                "subject": "Computer Vision",
            }
        )
        .execute()
    )
    if getattr(created, "error", None):
        raise RuntimeError(f"Could not create analysis job: {created.error}")

    upload = (
        supabase.storage.from_(BUCKET).upload(
            document_path,
            source,
            {"content-type": "application/pdf", "upsert": False},
        )
    )
    if upload.error:
        update_job(
            job_id,
            status="failed",
            stage="e2e_setup",
            progress=100,
            error=f"Fixture upload failed: {upload.error}",
        )
        raise RuntimeError(f"Fixture upload failed: {upload.error}")

    update_job(
        job_id,
        document_path=document_path,
        stage="dispatching",
        progress=5,
        error=None,
    )
    return job_id


def create_video_job(analysis_job_id: str, analysis: dict) -> str:
    job_id = str(uuid.uuid4())
    teacher_path = f"e2e/{job_id}/artifact2.json"
    visual_path = f"e2e/{job_id}/artifact3.json"
    amount = float(
        analysis.get("final_price")
        or analysis.get("pricing", {}).get("final_price")
        or 0
    )

    created = (
        supabase.table("jobs")
        .insert(
            {
                "id": job_id,
                "kind": "video",
                "status": "queued",
                "stage": "e2e_dispatching",
                "progress": 1,
                "quality": QUALITY,
                "target_minutes": TARGET_MINUTES,
                "payment_status": "test",
                "amount_paise": int(round(amount * 100)),
                "currency": "INR",
                "source_job_id": analysis_job_id,
                "tts_voice": TTS_VOICE,
                "tts_rate": "+0%",
                "tts_volume": "+0%",
            }
        )
        .execute()
    )
    if getattr(created, "error", None):
        raise RuntimeError(f"Could not create video job: {created.error}")

    teacher = analysis.get("teacher")
    visual = analysis.get("visual_design")
    if not isinstance(teacher, dict) or not isinstance(visual, dict):
        raise RuntimeError("Analysis did not produce teacher/visual artifacts.")

    for path, value in ((teacher_path, teacher), (visual_path, visual)):
        upload = (
            supabase.storage.from_(BUCKET).upload(
                path,
                json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"),
                {"content-type": "application/json", "upsert": False},
            )
        )
        if upload.error:
            update_job(
                job_id,
                status="failed",
                stage="e2e_setup",
                progress=100,
                error=f"Artifact upload failed: {upload.error}",
            )
            raise RuntimeError(f"Artifact upload failed: {upload.error}")

    update_job(
        job_id,
        teacher_path=teacher_path,
        visual_path=visual_path,
        status="queued",
        stage="dispatching",
        progress=2,
        error=None,
    )
    return job_id


def validate_video(output_path: str) -> dict:
    data = supabase.storage.from_(BUCKET).download(output_path)
    local = ROOT / ".e2e-final.mp4"
    local.write_bytes(data)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(local),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(probe.stdout)
    local.unlink(missing_ok=True)

    duration = float(metadata["format"]["duration"])
    size = int(metadata["format"]["size"])
    if size <= 100_000 or duration <= 1:
        raise RuntimeError(
            f"Rendered artifact is not credible: size={size}, duration={duration}"
        )
    return {"bytes": size, "duration_seconds": duration}


def main() -> None:
    ensure_bucket()
    fixture = load_fixture()

    print("=== AUTONOMOUS JUPITER E2E: ANALYSIS ===")
    analysis_job_id = create_analysis_job(fixture)
    print(f"ANALYSIS_JOB_ID={analysis_job_id}")

    github_dispatch(
        ANALYZE_WORKFLOW,
        {
            "job_id": analysis_job_id,
            "target_minutes": str(TARGET_MINUTES),
            "quality": QUALITY,
        },
    )

    analysis_job = wait_for_job(
        analysis_job_id,
        timeout_seconds=110 * 60,
        label="analysis",
    )

    if analysis_job.get("stage") != "ready_for_generation":
        raise RuntimeError(
            f"Analysis completed but is not generation-ready: {analysis_job.get('stage')}"
        )

    result_path = str(analysis_job.get("result_path") or "").strip()
    if not result_path:
        raise RuntimeError("Analysis completed without result_path.")

    analysis = download_json(result_path)
    if analysis.get("status") != "ready_for_generation":
        raise RuntimeError(
            f"Analysis artifact is not ready_for_generation: {analysis.get('status')}"
        )
    if (analysis.get("visual_validation") or {}).get("passed") is not True:
        raise RuntimeError("E2E analysis visual validation did not pass.")
    for key in ("teacher", "visual_design", "blueprint", "pricing"):
        if not analysis.get(key):
            raise RuntimeError(f"E2E analysis is missing artifact: {key}")

    print("=== AUTONOMOUS JUPITER E2E: RENDER ===")
    video_job_id = create_video_job(analysis_job_id, analysis)
    print(f"VIDEO_JOB_ID={video_job_id}")

    github_dispatch(
        RENDER_WORKFLOW,
        {
            "job_id": video_job_id,
            "target_minutes": str(TARGET_MINUTES),
            "quality": QUALITY,
            "tts_voice": TTS_VOICE,
            "tts_rate": "+0%",
            "tts_volume": "+0%",
        },
    )

    video_job = wait_for_job(
        video_job_id,
        timeout_seconds=180 * 60,
        label="render",
    )

    output_path = str(video_job.get("output_path") or "").strip()
    if not output_path:
        raise RuntimeError("Render completed without output_path.")

    video = validate_video(output_path)

    report = {
        "passed": True,
        "analysis_job_id": analysis_job_id,
        "analysis_result_path": result_path,
        "video_job_id": video_job_id,
        "video_output_path": output_path,
        "video": video,
        "fixture": {
            "source": "attached Vision Transformer sample PDF",
            "derived_pages": 3,
            "sha256": hashlib.sha256(fixture).hexdigest(),
        },
    }
    (ROOT / "e2e-report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print("========================================")
    print("JUPITER AUTONOMOUS E2E = PASS")
    print("========================================")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
