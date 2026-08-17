from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from supabase import create_client


# ============================================================
# CONFIGURATION
# ============================================================

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

if QUALITY == "premium":
    QUALITY = "elite"

SUPABASE_URL = os.environ[
    "SUPABASE_URL"
]

SUPABASE_KEY = os.environ[
    "SUPABASE_SERVICE_ROLE_KEY"
]

BUCKET = "jupiter-temp"


# ============================================================
# PATHS
# ============================================================

RUNNER_ROOT = (
    Path(__file__)
    .resolve()
    .parent
)

# GitHub Actions layout:
#
# jupiter-runner/
# ├── analyze_runner.py
# └── jupiter-core/
#
# Local layout:
#
# jupiter/
# ├── jupiter-runner/
# └── jupiter-core/

CORE_CANDIDATES = [
    RUNNER_ROOT / "jupiter-core",
    RUNNER_ROOT.parent / "jupiter-core",
]


CORE_ROOT: Path | None = None

for candidate in CORE_CANDIDATES:
    if (
        candidate.exists()
        and (
            candidate / "app"
        ).exists()
    ):
        CORE_ROOT = candidate
        break


if CORE_ROOT is None:
    searched = "\n".join(
        f"  - {p}"
        for p in CORE_CANDIDATES
    )

    raise RuntimeError(
        "Private jupiter-core repository was not found.\n"
        "Searched:\n"
        f"{searched}\n\n"
        "The GitHub Actions workflow must checkout "
        "jupiter-core into the runner workspace."
    )


CORE_PATH = str(
    CORE_ROOT
)

if CORE_PATH not in sys.path:
    sys.path.insert(
        0,
        CORE_PATH,
    )


# ============================================================
# PRIVATE JUPITER CORE IMPORTS
# ============================================================

from app.api.blueprint import (
    build_lesson_blueprint,
)

from app.parsers.pdf import (
    parse_pdf,
)

from app.intelligence.classifier import (
    classify_artifact1,
)

from app.intelligence.estimator import (
    estimate_credits,
)

from app.intelligence.llm_gateway import (
    gateway,
)

from app.intelligence.pricing import (
    calculate_production_price,
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


# ============================================================
# RUNTIME DIRECTORIES
# ============================================================

UPLOADS_DIR = (
    RUNNER_ROOT
    / "uploads"
)

UPLOADS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

JOBS_DIR = (
    RUNNER_ROOT
    / "jobs"
)

JOBS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# IMPORTANT:
# parse_pdf() currently writes paths such as:
#
# uploads/source-vit_p3_i0.png
#
# Therefore relative filesystem paths must resolve from the
# runner directory.
os.chdir(
    RUNNER_ROOT
)


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

print(
    f"RUNNER_ROOT    = {RUNNER_ROOT}"
)

print(
    f"CORE_ROOT      = {CORE_ROOT}"
)

print(
    f"UPLOADS_DIR    = {UPLOADS_DIR}"
)


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


# ============================================================
# DATABASE HELPERS
# ============================================================

def update_job(
    **values,
) -> None:
    (
        supabase
        .table("jobs")
        .update(values)
        .eq(
            "id",
            JOB_ID,
        )
        .execute()
    )


def get_job() -> dict:
    result = (
        supabase
        .table("jobs")
        .select("*")
        .eq(
            "id",
            JOB_ID,
        )
        .single()
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            f"Analysis job {JOB_ID} was not found."
        )

    return result.data


# ============================================================
# STORAGE HELPERS
# ============================================================

def download_object(
    remote_path: str,
    local_path: Path,
) -> None:
    data = (
        supabase
        .storage
        .from_(BUCKET)
        .download(
            remote_path
        )
    )

    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    local_path.write_bytes(
        data
    )


def upload_json(
    value: dict,
    remote_path: str,
) -> None:
    payload = json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
        default=str,
    ).encode(
        "utf-8"
    )

    (
        supabase
        .storage
        .from_(BUCKET)
        .upload(
            remote_path,
            payload,
            {
                "content-type": (
                    "application/json"
                ),
                "cache-control": (
                    "no-store"
                ),
                "upsert": "true",
            },
        )
    )


def delete_storage_object(
    remote_path: str,
) -> None:
    try:
        (
            supabase
            .storage
            .from_(BUCKET)
            .remove(
                [remote_path]
            )
        )
    except Exception as exc:
        print(
            "Storage cleanup warning:",
            exc,
        )


# ============================================================
# SOURCE METADATA
# ============================================================

def extract_page_count(
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
            if not isinstance(
                item,
                dict,
            ):
                continue

            page = item.get(
                "page"
            )

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
# LOCAL CLEANUP
# ============================================================

def cleanup_runtime_files() -> None:
    try:
        for path in UPLOADS_DIR.iterdir():
            if path.is_file():
                path.unlink(
                    missing_ok=True
                )

    except Exception as exc:
        print(
            "Uploads cleanup warning:",
            exc,
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

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
        and int(
            db_target_minutes
        )
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

    db_quality = (
        str(db_quality)
        .strip()
        .lower()
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
    # START
    # --------------------------------------------------------

    update_job(
        status="running",
        stage="downloading_source",
        progress=5,
        error=None,
    )

    print(
        f"Downloading source: {document_path}"
    )

    source = (
        RUNNER_ROOT
        / Path(
            document_path
        ).name
    )

    download_object(
        document_path,
        source,
    )

    print(
        f"Source file: {source}"
    )

    # --------------------------------------------------------
    # PARSE
    # --------------------------------------------------------

    update_job(
        stage="source_parsing",
        progress=10,
    )

    print(
        "Parsing source..."
    )

    suffix = (
        source.suffix
        .lower()
    )

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
            "Jupiter currently accepts PDF and TXT sources."
        )

    page_count = extract_page_count(
        artifact
    )

    print(
        f"Source pages = {page_count}"
    )

    # --------------------------------------------------------
    # RESET LLM USAGE ACCOUNTING
    # --------------------------------------------------------
    # Accounting only.
    # This does NOT alter token budgets, model selection,
    # retries, prompts, or worker contracts.
    # --------------------------------------------------------

    gateway.reset_usage_ledger()

    # --------------------------------------------------------
    # ARTIFACT 1
    # --------------------------------------------------------

    update_job(
        stage="source_analysis",
        progress=15,
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
            classification
        )
    )

    # --------------------------------------------------------
    # TEACHER
    # --------------------------------------------------------

    update_job(
        stage="teacher_plan",
        progress=35,
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
    # VISUAL DESIGN
    # --------------------------------------------------------

    update_job(
        stage="visual_design",
        progress=52,
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
    # VISUAL VALIDATION
    # --------------------------------------------------------

    update_job(
        stage="visual_validation",
        progress=67,
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
    # VISUAL REVIEW
    # --------------------------------------------------------

    visual_review = None

    if visual_validation.get(
        "passed"
    ):

        update_job(
            stage="visual_review",
            progress=77,
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
    # BLUEPRINT
    # --------------------------------------------------------

    update_job(
        stage="building_blueprint",
        progress=85,
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
    # FINAL PRODUCTION PRICE
    # --------------------------------------------------------
    # Pricing happens only after the complete analysis/
    # lesson blueprint exists.
    #
    # The pricing engine:
    #   - records actual LLM usage for accounting when available
    #   - estimates TTS cost
    #   - estimates render complexity
    #   - accounts for storage/delivery/operations
    #   - applies contingency
    #   - applies the configured gross-margin model
    #
    # It does NOT control any LLM token budget.
    # --------------------------------------------------------

    pricing = (
        calculate_production_price(
            artifact=classification,
            teacher_plan=teacher_plan,
            visual_design=visual_design,
            fact_ledger=fact_ledger,
            blueprint=blueprint,
            target_minutes=TARGET_MINUTES,
            quality=db_quality,
            llm_usage_records=(
                gateway.get_usage_ledger()
            ),
        )
    )

    print(
        "Final customer price = "
        f"{pricing.get('currency', 'INR')} "
        f"{pricing.get('final_price')}"
    )

    # --------------------------------------------------------
    # FINAL RESULT
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

        "pricing": pricing,

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

        "pricing_status": pricing.get(
            "status"
        ),

        "pricing_version": pricing.get(
            "pricing_version"
        ),

        "final_price": pricing.get(
            "final_price"
        ),

        "status": (
            "ready_for_generation"
            if visual_validation.get(
                "passed"
            )
            else "visual_design_rejected"
        ),
    }

    # --------------------------------------------------------
    # UPLOAD ANALYSIS RESULT
    # --------------------------------------------------------

    update_job(
        stage="uploading_result",
        progress=94,
    )

    result_path = (
        f"jobs/{JOB_ID}/analysis.json"
    )

    print(
        f"Uploading result: {result_path}"
    )

    upload_json(
        result,
        result_path,
    )

    # --------------------------------------------------------
    # SOURCE CLEANUP
    # --------------------------------------------------------

    delete_storage_object(
        document_path
    )

    cleanup_runtime_files()

    try:
        source.unlink(
            missing_ok=True
        )
    except Exception as exc:
        print(
            "Source file cleanup warning:",
            exc,
        )

    # --------------------------------------------------------
    # FINAL STATUS
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
    try:
        main()

    except Exception as exc:

        print(
            "========================================"
        )

        print(
            "      JUPITER ANALYSIS = FAILED"
        )

        print(
            "========================================"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        try:
            update_job(
                status="failed",
                stage="analysis_failed",
                progress=100,
                error=str(exc),
            )
        except Exception as db_exc:
            print(
                "Could not update failed job:",
                db_exc,
            )

        raise