from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


# ============================================================
# JUPITER RUNNER / PRIVATE CORE BOOTSTRAP
# ============================================================

RUNNER_ROOT = Path(__file__).resolve().parent
JUPITER_ROOT = RUNNER_ROOT.parent
CORE_ROOT = JUPITER_ROOT / "jupiter-core"

if not CORE_ROOT.exists():
    raise RuntimeError(
        "Private jupiter-core repository was not found.\n"
        f"Expected path: {CORE_ROOT}"
    )

CORE_PATH = str(CORE_ROOT)

if CORE_PATH not in sys.path:
    sys.path.insert(0, CORE_PATH)


# ============================================================
# IMPORT PRIVATE JUPITER CORE
# ============================================================

from app.parsers.pdf import parse_pdf

from app.intelligence.classifier import (
    classify_artifact1,
)

from app.intelligence.estimator import (
    estimate_credits,
)

from app.intelligence.teacher import (
    create_teaching_plan,
)

from app.intelligence.teacher_reviewer import (
    review_teaching_plan,
)

from app.intelligence.visual_designer import (
    create_visual_design,
)

from app.intelligence.visual_reviewer import (
    review_visual_design,
)

from app.intelligence.fact_ledger import (
    build_fact_ledger,
)

from app.intelligence.visual_validator import (
    validate_visual_design,
)

from app.api.blueprint import (
    build_lesson_blueprint,
)


JOB_ID = os.environ["JOB_ID"]

TARGET_MINUTES = int(
    os.environ.get(
        "TARGET_MINUTES",
        "5",
    )
)

QUALITY = (
    os.environ.get(
        "QUALITY",
        "normal",
    )
    .strip()
    .lower()
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

BUCKET = "jupiter-temp"

from supabase import create_client

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


# ============================================================
# RUNTIME DIRECTORIES
# ============================================================

# IMPORTANT:
# The PDF parser creates extracted images such as:
#
# uploads/source-vit_p3_i0.png
#
# GitHub Actions starts with a clean workspace, so this directory
# MUST exist before parse_pdf() is called.

UPLOADS_DIR = RUNNER_ROOT / "uploads"
UPLOADS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

JOBS_DIR = RUNNER_ROOT / "jobs"
JOBS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

# Ensure relative paths inside the parser resolve from the runner
# root regardless of the shell/workflow working directory.
os.chdir(RUNNER_ROOT)

print(
    f"Runner root: {RUNNER_ROOT}"
)
print(
    f"Core root: {CORE_ROOT}"
)
print(
    f"Uploads directory: {UPLOADS_DIR}"
)


# ============================================================
# SUPABASE HELPERS
# ============================================================

def update_job(**values) -> None:
    (
        supabase
        .table("jobs")
        .update(values)
        .eq("id", JOB_ID)
        .execute()
    )


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
            f"Analysis job {JOB_ID} not found."
        )

    return result.data


def download_object(
    remote_path: str,
    local_path: Path,
) -> None:
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

    local_path.write_bytes(data)


def upload_json(
    value: dict,
    remote_path: str,
) -> None:
    payload = json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    (
        supabase
        .storage
        .from_(BUCKET)
        .upload(
            remote_path,
            payload,
            {
                "content-type": "application/json",
                "cache-control": "no-store",
                "upsert": "true",
            },
        )
    )


# ============================================================
# SOURCE METADATA
# ============================================================

def _extract_page_count(
    artifact: dict,
) -> int:
    document = artifact.get(
        "document",
        {},
    )

    page_count = document.get(
        "page_count"
    )

    if (
        isinstance(
            page_count,
            int,
        )
        and page_count > 0
    ):
        return page_count

    pages: set[int] = set()

    for collection_name in (
        "content_blocks",
        "images",
        "tables",
        "equations",
    ):
        for item in artifact.get(
            collection_name,
            [],
        ):
            page = item.get("page")

            if isinstance(
                page,
                int,
            ):
                pages.add(page)

    return max(
        pages,
        default=0,
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print(
        "========================================"
    )
    print(
        "       JUPITER CLOUD ANALYSIS"
    )
    print(
        "========================================"
    )
    print(
        f"JOB_ID         = {JOB_ID}"
    )
    print(
        f"TARGET_MINUTES = {TARGET_MINUTES}"
    )
    print(
        f"QUALITY        = {QUALITY}"
    )

    job = get_job()

    document_path = job.get(
        "document_path"
    )

    if not document_path:
        raise RuntimeError(
            "Analysis job has no document_path."
        )

    db_target_minutes = job.get(
        "target_minutes"
    )

    if (
        db_target_minutes is not None
        and int(db_target_minutes)
        != TARGET_MINUTES
    ):
        raise RuntimeError(
            "Duration mismatch: "
            f"database={db_target_minutes}, "
            f"workflow={TARGET_MINUTES}"
        )

    db_quality = (
        job.get("quality")
        or QUALITY
    )

    if db_quality == "premium":
        db_quality = "elite"

    if db_quality not in {
        "normal",
        "elite",
    }:
        raise RuntimeError(
            f"Invalid quality: {db_quality}"
        )

    # --------------------------------------------------------
    # Download source
    # --------------------------------------------------------

    update_job(
        status="running",
        stage="source_parsing",
        progress=10,
    )

    source = (
        RUNNER_ROOT
        / Path(document_path).name
    )

    print(
        f"Downloading source: {document_path}"
    )

    download_object(
        document_path,
        source,
    )

    print(
        f"Source file: {source.name}"
    )

    # --------------------------------------------------------
    # Parse source
    # --------------------------------------------------------

    print(
        f"Uploads directory exists: "
        f"{UPLOADS_DIR.exists()}"
    )

    print(
        "Parsing source document..."
    )

    suffix = source.suffix.lower()

    if suffix == ".pdf":

        artifact = parse_pdf(
            source
        )

    elif suffix == ".txt":

        text = source.read_text(
            encoding="utf-8",
            errors="replace",
        )

        artifact = {
            "document": {
                "type": "txt",
                "title": source.stem,
                "language": "unknown",
                "page_count": 1,
            },
            "content_blocks": [
                {
                    "id": "text_1",
                    "page": 1,
                    "type": "text",
                    "text": text,
                    "importance": "core",
                }
            ],
            "images": [],
            "tables": [],
            "equations": [],
            "concepts": [],
        }

    else:
        raise RuntimeError(
            "MVP accepts PDF and TXT files only."
        )

    page_count = _extract_page_count(
        artifact
    )

    print(
        f"Source pages = {page_count}"
    )

    # --------------------------------------------------------
    # Artifact 1 / classification
    # --------------------------------------------------------

    update_job(
        stage="source_analysis",
        progress=20,
    )

    print(
        "Running Artifact 1 classification..."
    )

    classification = (
        classify_artifact1(
            artifact,
            db_quality,
        )
    )

    print(
        "Estimating credits..."
    )

    estimate = (
        estimate_credits(
            classification,
            TARGET_MINUTES,
            db_quality,
        )
    )

    print(
        "Building fact ledger..."
    )

    fact_ledger = (
        build_fact_ledger(
            classification,
        )
    )

    # --------------------------------------------------------
    # Teacher
    # --------------------------------------------------------

    update_job(
        stage="teacher_plan",
        progress=40,
    )

    print(
        "Generating teacher plan..."
    )

    teacher_plan = (
        create_teaching_plan(
            classification,
            TARGET_MINUTES,
            db_quality,
        )
    )

    print(
        "Reviewing teacher plan..."
    )

    teacher_review = (
        review_teaching_plan(
            classification,
            teacher_plan,
            db_quality,
        )
    )

    # --------------------------------------------------------
    # Visual Design
    # --------------------------------------------------------

    update_job(
        stage="visual_design",
        progress=58,
    )

    print(
        "Generating visual design..."
    )

    visual_design = (
        create_visual_design(
            teacher_plan,
            TARGET_MINUTES,
            db_quality,
            fact_ledger=fact_ledger,
        )
    )

    # --------------------------------------------------------
    # Visual validation
    # --------------------------------------------------------

    update_job(
        stage="visual_validation",
        progress=72,
    )

    print(
        "Validating visual design..."
    )

    visual_validation = (
        validate_visual_design(
            visual_design,
            fact_ledger,
            teacher_plan,
        )
    )

    # --------------------------------------------------------
    # Visual review
    # --------------------------------------------------------

    visual_review = None

    if visual_validation.get(
        "passed"
    ):

        update_job(
            stage="visual_review",
            progress=82,
        )

        print(
            "Reviewing visual design..."
        )

        visual_review = (
            review_visual_design(
                teacher_plan,
                visual_design,
                db_quality,
            )
        )

    # --------------------------------------------------------
    # Lesson Blueprint
    #
    # No AI call.
    # No additional credits.
    # --------------------------------------------------------

    update_job(
        stage="building_blueprint",
        progress=88,
    )

    print(
        "Building lesson blueprint..."
    )

    blueprint = (
        build_lesson_blueprint(
            artifact=classification,
            teaching_plan=teacher_plan,
            visual_design=visual_design,
            target_minutes=TARGET_MINUTES,
            quality=db_quality,
            narrator=job.get(
                "tts_voice"
            ),
            generation_price=None,
        )
    )

    # --------------------------------------------------------
    # Final analysis result
    # --------------------------------------------------------

    result = {
        "project_id": JOB_ID,

        "artifact": classification,

        "estimate": estimate,

        "teacher": teacher_plan,

        "teacher_review": teacher_review,

        "fact_ledger": fact_ledger,

        "visual_design": visual_design,

        "visual_validation": visual_validation,

        "visual_review": visual_review,

        "blueprint": blueprint,

        "source_metadata": {
            "page_count": page_count,
            "tables": len(
                classification.get(
                    "tables",
                    [],
                )
            ),
            "images": len(
                classification.get(
                    "images",
                    [],
                )
            ),
            "equations": len(
                classification.get(
                    "equations",
                    [],
                )
            ),
        },

        "target_minutes": TARGET_MINUTES,

        "quality": db_quality,

        "status": (
            "ready_for_generation"
            if visual_validation.get(
                "passed"
            )
            else "visual_design_rejected"
        ),
    }

    # --------------------------------------------------------
    # Upload result
    # --------------------------------------------------------

    result_path = (
        f"jobs/{JOB_ID}/analysis.json"
    )

    update_job(
        stage="uploading_result",
        progress=94,
    )

    print(
        f"Uploading result: {result_path}"
    )

    upload_json(
        result,
        result_path,
    )

    # --------------------------------------------------------
    # Cleanup source
    # --------------------------------------------------------

    try:
        (
            supabase
            .storage
            .from_(BUCKET)
            .remove(
                [
                    document_path
                ]
            )
        )

    except Exception as exc:
        print(
            "Source cleanup warning:",
            exc,
        )

    # --------------------------------------------------------
    # Cleanup parser-generated uploads
    # --------------------------------------------------------

    try:
        for path in UPLOADS_DIR.glob("*"):
            if path.is_file():
                path.unlink(
                    missing_ok=True
                )

    except Exception as exc:
        print(
            "Uploads cleanup warning:",
            exc,
        )

    # --------------------------------------------------------
    # Final state
    # --------------------------------------------------------

    final_stage = (
        "ready_for_generation"
        if visual_validation.get(
            "passed"
        )
        else "visual_design_rejected"
    )

    update_job(
        status="completed",
        stage=final_stage,
        progress=100,
        result_path=result_path,
        error=None,
    )

    print(
        "========================================"
    )
    print(
        "      JUPITER ANALYSIS = SUCCESS"
    )
    print(
        "========================================"
    )
    print(
        f"RESULT = {result_path}"
    )


if __name__ == "__main__":
    main()