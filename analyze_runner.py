from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

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

    if getattr(
        result,
        "error",
        None,
    ):
        raise RuntimeError(
            f"Result upload failed: {result.error}"
        )


def main() -> None:
    print(
        f"JUPITER ANALYSIS JOB {JOB_ID}"
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

    if db_quality not in {
        "normal",
        "elite",
    }:
        raise RuntimeError(
            f"Invalid quality: {db_quality}"
        )

    # Private core is checked out beside this runner.
    core = (
        Path(__file__).resolve().parent
        / "jupiter-core"
    )

    if not core.exists():
        raise RuntimeError(
            "Private jupiter-core was not checked out."
        )

    sys.path.insert(
        0,
        str(core),
    )

    # Import the existing private-core pipeline.
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

    with tempfile.TemporaryDirectory(
        prefix=(
            f"jupiter-analysis-{JOB_ID}-"
        ),
    ) as temp_dir:

        work = Path(temp_dir)

        source = (
            work /
            Path(document_path).name
        )

        update_job(
            status="running",
            stage="downloading_source",
            progress=5,
        )

        download_object(
            document_path,
            source,
        )

        suffix = source.suffix.lower()

        if suffix == ".pdf":
            artifact = parse_pdf(source)

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
                },
                "content_blocks": [
                    {
                        "id": "text_1",
                        "page": None,
                        "type": "text",
                        "text": text,
                        "importance": (
                            "unclassified"
                        ),
                    }
                ],
                "images": [],
                "tables": [],
                "equations": [],
                "concepts": [],
            }

        else:
            raise RuntimeError(
                "MVP currently accepts PDF and TXT."
            )

        # Artifact 1
        update_job(
            stage="source_analysis",
            progress=12,
        )

        classification = (
            classify_artifact1(
                artifact,
                db_quality,
            )
        )

        estimate = (
            estimate_credits(
                classification,
                TARGET_MINUTES,
                db_quality,
            )
        )

        fact_ledger = (
            build_fact_ledger(
                classification,
            )
        )

        # Artifact 2
        update_job(
            stage="teacher_plan",
            progress=30,
        )

        teacher_plan = (
            create_teaching_plan(
                classification,
                TARGET_MINUTES,
                db_quality,
            )
        )

        teacher_review = (
            review_teaching_plan(
                classification,
                teacher_plan,
                db_quality,
            )
        )

        # Artifact 3
        update_job(
            stage="visual_design",
            progress=50,
        )

        visual_design = (
            create_visual_design(
                teacher_plan,
                TARGET_MINUTES,
                db_quality,
                fact_ledger=fact_ledger,
            )
        )

        update_job(
            stage="visual_validation",
            progress=65,
        )

        visual_validation = (
            validate_visual_design(
                visual_design,
                fact_ledger,
                teacher_plan,
            )
        )

        visual_review = None

        if visual_validation.get(
            "passed"
        ):
            update_job(
                stage="visual_review",
                progress=75,
            )

            visual_review = (
                review_visual_design(
                    teacher_plan,
                    visual_design,
                    db_quality,
                )
            )

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
            "status": (
                "ready_for_generation"
                if visual_validation.get(
                    "passed"
                )
                else "visual_design_rejected"
            ),
            "target_minutes": TARGET_MINUTES,
            "quality": db_quality,
        }

        result_path = (
            f"jobs/{JOB_ID}/analysis.json"
        )

        update_job(
            stage="uploading_result",
            progress=90,
        )

        upload_json(
            result,
            result_path,
        )

        # Delete the source PDF/TXT after analysis.
        try:
            (
                supabase
                .storage
                .from_(BUCKET)
                .remove([
                    document_path
                ])
            )
        except Exception as exc:
            print(
                "Source cleanup warning:",
                exc,
            )

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
            "JUPITER ANALYSIS = SUCCESS"
        )

        print(
            f"RESULT = {result_path}"
        )


if __name__ == "__main__":
    main()