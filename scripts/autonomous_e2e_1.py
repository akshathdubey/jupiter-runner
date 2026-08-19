from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from supabase import create_client

BASE_URL = os.environ.get("JUPITER_E2E_BASE_URL", "https://jupiter.zenithxlabs.com").rstrip("/")
OWNER = os.environ.get("GITHUB_OWNER", "akshathdubey")
REPO = os.environ.get("GITHUB_REPO", "jupiter-runner")
RENDER_WORKFLOW = os.environ.get("GITHUB_RENDER_WORKFLOW", "render.yml")
REF = "main"
TARGET_MINUTES = int(os.environ.get("JUPITER_E2E_MINUTES", "3"))
QUALITY = os.environ.get("JUPITER_E2E_QUALITY", "normal")
TTS_VOICE = os.environ.get("JUPITER_E2E_VOICE", "en-IN-NeerjaNeural")
TEST_EMAIL = "dubeyakshath19@gmail.com"
BUCKET = "jupiter-temp"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / ".e2e-vit-fixture.pdf"
REPORT = ROOT / "e2e-report.json"
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def multipart_upload(url: str, fields: dict[str, str], filename: str, data: bytes) -> dict:
    boundary = f"----JupiterE2E{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode(), b"\r\n"])
    chunks.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(), b"Content-Type: application/pdf\r\n\r\n", data, b"\r\n", f"--{boundary}--\r\n".encode()])
    request = urllib.request.Request(url, method="POST", data=b"".join(chunks), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} from {url}: {error.read().decode('utf-8', errors='replace')}") from error


def github_dispatch(inputs: dict[str, str]) -> None:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/{RENDER_WORKFLOW}/dispatches"
    request = urllib.request.Request(url, method="POST", data=json.dumps({"ref": REF, "inputs": inputs}).encode(), headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 204:
                raise RuntimeError(f"GitHub dispatch failed: HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub dispatch failed: HTTP {error.code}: {error.read().decode(errors='replace')}") from error


def get_job(job_id: str) -> dict:
    result = supabase.table("jobs").select("*").eq("id", job_id).single().execute()
    if not result.data:
        raise RuntimeError(f"Job {job_id} was not found.")
    return result.data


def wait_for_job(job_id: str, timeout_seconds: int, label: str) -> dict:
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        job = get_job(job_id)
        marker = (job.get("status"), job.get("stage"), job.get("progress"))
        if marker != last:
            print(f"[{label}] status={marker[0]} stage={marker[1]} progress={marker[2]}")
            last = marker
        if marker[0] == "completed":
            return job
        if marker[0] in {"failed", "cancelled"}:
            raise RuntimeError(f"{label} job failed: stage={job.get('stage')} error={job.get('error')}")
        time.sleep(10)
    raise TimeoutError(f"{label} job {job_id} did not finish in time.")


def create_fixture() -> bytes:
    pdf = canvas.Canvas(str(FIXTURE), pagesize=A4)
    width, height = A4
    pages = [
        ("Vision Transformers — E2E Fixture", ["A Vision Transformer applies the transformer architecture to images.", "The image is split into fixed-size patches, and each patch becomes a token.", "A learnable class token can summarize the image for classification."]),
        ("Patch Embeddings and Attention", ["Patch embeddings map image patches into a common vector space.", "Self-attention lets every patch interact with every other patch.", "Multi-head attention captures different relationships between visual tokens."]),
        ("Why the Transformer Works", ["Positional information retains token order and spatial structure.", "Repeated transformer blocks refine representations through attention and MLP layers.", "The final representation can be used for image classification."]),
    ]
    for title, lines in pages:
        pdf.setFont("Helvetica-Bold", 22); pdf.drawString(54, height - 70, title)
        pdf.setFont("Helvetica", 13); y = height - 120
        for line in lines: pdf.drawString(54, y, line); y -= 34
        pdf.setFont("Helvetica-Oblique", 10); pdf.drawString(54, 54, "Jupiter autonomous E2E workflow #1"); pdf.showPage()
    pdf.save()
    data = FIXTURE.read_bytes()
    if len(data) < 1000: raise RuntimeError("Generated E2E PDF fixture is unexpectedly small.")
    return data


def start_analysis(fixture: bytes) -> str:
    response = multipart_upload(f"{BASE_URL}/api/documents/analyze", {"target_minutes": str(TARGET_MINUTES), "quality": QUALITY, "subject": "Computer Vision"}, "vit-e2e-fixture.pdf", fixture)
    job_id = str(response.get("job_id") or "").strip()
    if not job_id: raise RuntimeError(f"Analysis API did not return a job_id: {response}")
    print(f"ANALYSIS_JOB_ID={job_id}")
    return job_id


def load_analysis(job: dict) -> dict:
    result_path = str(job.get("result_path") or "").strip()
    if not result_path: raise RuntimeError("Analysis completed without result_path.")
    analysis = json.loads(supabase.storage.from_(BUCKET).download(result_path).decode("utf-8"))
    if analysis.get("status") != "ready_for_generation": raise RuntimeError(f"Analysis artifact is not generation-ready: {analysis.get('status')}")
    if (analysis.get("visual_validation") or {}).get("passed") is not True: raise RuntimeError("Analysis visual validation did not pass.")
    for key in ("teacher", "visual_design", "blueprint", "pricing"):
        if not analysis.get(key): raise RuntimeError(f"Analysis artifact is missing {key}.")
    return analysis


def create_render_job(analysis_job_id: str, analysis: dict) -> str:
    job_id = str(uuid.uuid4())
    teacher_path = f"e2e/{job_id}/artifact2.json"; visual_path = f"e2e/{job_id}/artifact3.json"
    created = supabase.table("jobs").insert({"id": job_id, "kind": "video", "status": "queued", "stage": "e2e_dispatching", "progress": 1, "quality": QUALITY, "target_minutes": TARGET_MINUTES, "payment_status": "test", "amount_paise": 0, "currency": "INR", "source_job_id": analysis_job_id, "customer_email": TEST_EMAIL, "tts_voice": TTS_VOICE, "tts_rate": "+0%", "tts_volume": "+0%"}).execute()
    if getattr(created, "error", None): raise RuntimeError(f"Could not create E2E video job: {created.error}")
    for path, value in ((teacher_path, analysis["teacher"]), (visual_path, analysis["visual_design"])):
        upload = supabase.storage.from_(BUCKET).upload(path, json.dumps(value, ensure_ascii=False).encode("utf-8"), {"content-type": "application/json", "upsert": False})
        if upload.error: raise RuntimeError(f"Could not upload {path}: {upload.error}")
    updated = supabase.table("jobs").update({"teacher_path": teacher_path, "visual_path": visual_path, "stage": "dispatching", "progress": 2, "error": None}).eq("id", job_id).execute()
    if getattr(updated, "error", None): raise RuntimeError(f"Could not finalize E2E video job: {updated.error}")
    print(f"VIDEO_JOB_ID={job_id}")
    return job_id


def validate_video(output_path: str) -> dict:
    raw = supabase.storage.from_(BUCKET).download(output_path)
    video_path = ROOT / ".e2e-final.mp4"; video_path.write_bytes(raw)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size,format_name", "-of", "json", str(video_path)], check=True, capture_output=True, text=True)
    video_path.unlink(missing_ok=True)
    metadata = json.loads(probe.stdout)["format"]
    duration = float(metadata["duration"]); size = int(metadata["size"]); format_name = str(metadata.get("format_name") or "")
    if duration < 1 or size < 100_000 or "mp4" not in format_name: raise RuntimeError(f"Rendered artifact is not credible: {metadata}")
    return {"duration_seconds": duration, "bytes": size, "format": format_name}


def main() -> None:
    started = time.time(); fixture = create_fixture(); print(f"Fixture bytes={len(fixture)}")
    health = http_json(f"{BASE_URL}/api/health")
    if health.get("status") != "ok": raise RuntimeError(f"Jupiter health check failed: {health}")
    analysis_job_id = start_analysis(fixture); analysis_job = wait_for_job(analysis_job_id, 90 * 60, "analysis"); analysis = load_analysis(analysis_job)
    video_job_id = create_render_job(analysis_job_id, analysis)
    github_dispatch({"job_id": video_job_id, "target_minutes": str(TARGET_MINUTES), "quality": QUALITY, "tts_voice": TTS_VOICE, "tts_rate": "+0%", "tts_volume": "+0%"})
    video_job = wait_for_job(video_job_id, 180 * 60, "render")
    output_path = str(video_job.get("output_path") or "").strip()
    if not output_path: raise RuntimeError("Render completed without output_path.")
    video = validate_video(output_path)
    report = {"workflow": "autonomous-e2e-1", "passed": True, "base_url": BASE_URL, "analysis_job_id": analysis_job_id, "video_job_id": video_job_id, "analysis_result_path": analysis_job.get("result_path"), "video_output_path": output_path, "video": video, "fixture": {"type": "pdf", "bytes": len(fixture)}, "elapsed_seconds": round(time.time() - started, 2)}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8"); print(json.dumps(report, indent=2)); FIXTURE.unlink(missing_ok=True)


if __name__ == "__main__":
    try: main()
    except Exception as error:
        print(f"JUPITER AUTONOMOUS E2E #1 FAILED: {error}")
        REPORT.write_text(json.dumps({"workflow": "autonomous-e2e-1", "passed": False, "error": str(error)}, indent=2), encoding="utf-8")
        raise
