from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from supabase import create_client


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

SUPABASE_URL = os.environ[
    "SUPABASE_URL"
]

SUPABASE_KEY = os.environ[
    "SUPABASE_SERVICE_ROLE_KEY"
]

BUCKET = "jupiter-temp"

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
)


def update_job(**values: Any) -> None:
    result = (
        supabase
        .table("jobs")
        .update(values)
        .eq("id", JOB_ID)
        .execute()
    )

    error = getattr(
        result,
        "error",
        None,
    )

    if error:
        raise RuntimeError(
            f"Supabase job update failed: {error}"
        )


def get_job() -> dict[str, Any]:
    result = (
        supabase
        .table("jobs")
        .select("*")
        .eq("id", JOB_ID)
        .single()
        .execute()
    )

    error = getattr(
        result,
        "error",
        None,
    )

    if error:
        raise RuntimeError(
            f"Could not read analysis job: {error}"
        )

    if not result.data:
        raise RuntimeError(
            f"Analysis job {JOB_ID} not found."
        )

    return dict(result.data)


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

    if not isinstance(
        data,
        bytes,
    ):
        raise RuntimeError(
            "Supabase storage returned invalid data."
        )

    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    local_path.write_bytes(
        data,
    )


def upload_json(
    value: dict[str, Any],
    remote_path: str,
) -> None:
    payload = json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    result = (
        supabase
        .storage
        .from_(BUCKET)
        .upload(
            remote_path,
            payload,
            {
                "content-type":
                    "application/json",
                "cache-control":
                    "no-store",
                "upsert":
                    "true",
            },
        )
    )

    error = getattr(
        result,
        "error",
        None,
    )

    if error:
        raise RuntimeError(
            f"Result upload failed: {error}"
        )


def delete_source(
    remote_path: str,
) -> None:
    try:
        result = (
            supabase
            .storage
            .from_(BUCKET)
            .remove(
                [remote_path]
            )
        )

        error = getattr(
            result,
            "error",
            None,
        )

        if error:
            print(
                "Source cleanup warning:",
                error,
            )

    except Exception as exc:
        print(
            "Source cleanup warning:",
            exc,
        )


def import_private_core() -> None:
    core_path = (
        Path(__file__).resolve().parent
        / "jupiter-core"
    )

    if not core_path.exists():
        raise RuntimeError(
            "Private jupiter-core was not checked out."
        )

    app_path = (
        core_path /
        "app"
    )

    if not app_path.exists():
        raise RuntimeError(
            "Private jupiter-core/app directory is missing."
        )

    core_string = str(core_path)

    if core_string not in sys.path:
        sys.path.insert(
            0,
            core_string,
        )


def cleanup_workdir(
    path: Path,
) -> None:
    """
    Best-effort cleanup.

    On Windows, PyMuPDF may still hold an open PDF handle
    after parsing. The analysis result is already persisted,
    so cleanup must never turn a successful job into a failed job.
    """
    try:
        shutil.rmtree(
            path,
            ignore_errors=True,
        )
    except Exception as exc:
        print(
            "Temporary cleanup warning:",
            exc,
        )


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

    if QUALITY not in {
        "normal",
        "elite",
    }:
        raise RuntimeError(
            f"Invalid quality: {QUALITY}"
        )

    if TARGET_MINUTES not in {
        1,
        3,
        5,
        10,
        15,
        30,
        60,
    }:
        raise RuntimeError(
            f"Invalid target duration: {TARGET_MINUTES}"
        )

    job = get_job()

    document_path = job.get(
        "document_path",
    )

    if not document_path:
        raise RuntimeError(
            "Analysis job has no document_path."
        )

    database_minutes = job.get(
        "target_minutes",
    )

    if (
        database_minutes is not None
        and int(database_minutes)
        != TARGET_MINUTES
    ):
        raise RuntimeError(
            "Duration mismatch: "
            f"database={database_minutes}, "
            f"workflow={TARGET_MINUTES}"
        )

    database_quality = (
        job.get("quality")
        or QUALITY
    )

    if database_quality not in {
        "normal",
        "elite",
    }:
        raise RuntimeError(
            f"Invalid database quality: "
            f"{database_quality}"
        )

    if (
        database_quality
        != QUALITY
    ):
        raise RuntimeError(
            "Quality mismatch: "
            f"database={database_quality}, "
            f"workflow={QUALITY}"
        )

    import_private_core()

    from app.parsers.pdf import (
        parse_pdf,
    )

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

    work_dir: Path | None = None
    analysis_succeeded = False

    original_cwd = Path.cwd()

    try:
        work_dir = Path(
            tempfile.mkdtemp(
                prefix=(
                    f"jupiter-analysis-{JOB_ID}-"
                ),
            )
        )

        uploads_dir = (
            work_dir /
            "uploads"
        )

        uploads_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        os.chdir(
            work_dir,
        )

        source_path = (
            work_dir /
            Path(document_path).name
        )

        update_job(
            status="running",
            stage="downloading_source",
            progress=5,
            error=None,
        )

        print(
            f"Downloading source: {document_path}"
        )

        download_object(
            document_path,
            source_path,
        )

        print(
            f"Source file: {source_path.name}"
        )

        suffix = (
            source_path.suffix
            .lower()
        )

        # -------------------------------------------------
        # SOURCE PARSING
        # -------------------------------------------------

        update_job(
            stage="source_parsing",
            progress=10,
        )

        if suffix == ".pdf":
            artifact = parse_pdf(
                source_path
            )

        elif suffix == ".txt":
            text = (
                source_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )

            artifact = {
                "document": {
                    "type":
                        "txt",
                    "title":
                        source_path.stem,
                    "language":
                        "unknown",
                },

                "content_blocks": [
                    {
                        "id":
                            "text_1",
                        "page":
                            None,
                        "type":
                            "text",
                        "text":
                            text,
                        "importance":
                            "unclassified",
                    }
                ],

                "images": [],
                "tables": [],
                "equations": [],
                "concepts": [],
            }

        else:
            raise RuntimeError(
                "Unsupported source type. "
                "Only PDF and TXT are supported."
            )

        # -------------------------------------------------
        # ARTIFACT 1
        # -------------------------------------------------

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
                database_quality,
            )
        )

        print(
            "Estimating credits..."
        )

        estimate = (
            estimate_credits(
                classification,
                TARGET_MINUTES,
                database_quality,
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

        # -------------------------------------------------
        # ARTIFACT 2
        # -------------------------------------------------

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
                database_quality,
            )
        )

        print(
            "Reviewing teacher plan..."
        )

        teacher_review = (
            review_teaching_plan(
                classification,
                teacher_plan,
                database_quality,
            )
        )

        # -------------------------------------------------
        # ARTIFACT 3
        # -------------------------------------------------

        update_job(
            stage="visual_design",
            progress=55,
        )

        print(
            "Generating visual design..."
        )

        visual_design = (
            create_visual_design(
                teacher_plan,
                TARGET_MINUTES,
                database_quality,
                fact_ledger=fact_ledger,
            )
        )

        # -------------------------------------------------
        # VISUAL VALIDATION
        # -------------------------------------------------

        update_job(
            stage="visual_validation",
            progress=70,
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

        # -------------------------------------------------
        # VISUAL REVIEW
        # -------------------------------------------------

        visual_review = None

        if visual_validation.get(
            "passed",
        ):
            update_job(
                stage="visual_review",
                progress=80,
            )

            print(
                "Running visual review..."
            )

            visual_review = (
                review_visual_design(
                    teacher_plan,
                    visual_design,
                    database_quality,
                )
            )

        else:
            print(
                "Visual validation failed; "
                "skipping visual review."
            )

        # -------------------------------------------------
        # FINAL RESULT
        # -------------------------------------------------

        ready_for_generation = bool(
            visual_validation.get(
                "passed",
            )
        )

        result = {
            "project_id":
                JOB_ID,

            "artifact":
                classification,

            "estimate":
                estimate,

            "teacher":
                teacher_plan,

            "teacher_review":
                teacher_review,

            "fact_ledger":
                fact_ledger,

            "visual_design":
                visual_design,

            "visual_validation":
                visual_validation,

            "visual_review":
                visual_review,

            "status":
                (
                    "ready_for_generation"
                    if ready_for_generation
                    else "visual_design_rejected"
                ),

            "target_minutes":
                TARGET_MINUTES,

            "quality":
                database_quality,
        }

        result_path = (
            f"jobs/{JOB_ID}/analysis.json"
        )

        update_job(
            stage="uploading_result",
            progress=90,
        )

        print(
            f"Uploading result: {result_path}"
        )

        upload_json(
            result,
            result_path,
        )

        # Source document is no longer needed.
        delete_source(
            document_path,
        )

        final_stage = (
            "ready_for_generation"
            if ready_for_generation
            else "visual_design_rejected"
        )

        update_job(
            status="completed",
            stage=final_stage,
            progress=100,
            result_path=result_path,
            error=None,
        )

        analysis_succeeded = True

        print(
            "========================================"
        )

        print(
            "     JUPITER ANALYSIS = SUCCESS"
        )

        print(
            "========================================"
        )

        print(
            f"RESULT = {result_path}"
        )

    except Exception as exc:
        if not analysis_succeeded:
            error_message = (
                f"{type(exc).__name__}: {exc}"
            )

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
                error_message
            )

            try:
                update_job(
                    status="failed",
                    stage="analysis_failed",
                    progress=100,
                    error=error_message,
                )
            except Exception as update_error:
                print(
                    "Could not update failed job:",
                    update_error,
                )

            raise

    finally:
        try:
            os.chdir(
                original_cwd,
            )
        except Exception:
            pass

        if work_dir is not None:
            cleanup_workdir(
                work_dir,
            )


if __name__ == "__main__":
    main()