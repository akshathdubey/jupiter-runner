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
                "content-type": "application/json",
                "cache-control": "no-store",
                "upsert": "true",
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


def _extract_page_count(
    artifact: dict,
) -> int:
    """
    Get page count from the parser when available.

    Falls back to the highest page number found in
    extracted source objects.
    """

    document = artifact.get(
        "document",
        {},
    )

    page_count = document.get(
        "page_count"
    )

    if isinstance(
        page_count,
        int,
    ) and page_count > 0:
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

    if db_quality not in {
        "normal",
        "elite",
        "premium",
    }:
        raise RuntimeError(
            f"Invalid quality: {db_quality}"
        )

    # ------------------------------------------------------------
    # Private jupiter-core
    # ------------------------------------------------------------

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

    # ------------------------------------------------------------
    # Existing private-core pipeline
    # ------------------------------------------------------------

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

    # ------------------------------------------------------------
    # NEW: user-facing lesson blueprint
    # ------------------------------------------------------------

    from app.api.blueprint import (
        build_lesson_blueprint,
    )

    # ------------------------------------------------------------
    # Temporary working directory
    # ------------------------------------------------------------

    with tempfile.TemporaryDirectory(
        prefix=(
            f"jupiter-analysis-{JOB_ID}-"
        ),
    ) as temp_dir:

        work = Path(temp_dir)

        source = (
            work
            / Path(document_path).name
        )

        # --------------------------------------------------------
        # Download source
        # --------------------------------------------------------

        update_job(
            status="running",
            stage="downloading_source",
            progress=5,
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

        update_job(
            stage="source_parsing",
            progress=10,
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
                        "page": None,
                        "type": "text",
                        "text": text,
                        "importance": "unclassified",
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

        page_count = _extract_page_count(
            artifact
        )

        print(
            f"Source pages = {page_count}"
        )

        # --------------------------------------------------------
        # Artifact 1
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
                classification,
            )
        )

        # --------------------------------------------------------
        # Artifact 2
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
        # Artifact 3
        # --------------------------------------------------------

        update_job(
            stage="visual_design",
            progress=50,
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
            progress=65,
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
                progress=75,
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
        # NEW: build user-facing blueprint
        #
        # IMPORTANT:
        # This does NOT call an LLM.
        # It only transforms already-generated artifacts.
        # Therefore it does not consume additional AI credits.
        # --------------------------------------------------------

        update_job(
            stage="building_blueprint",
            progress=82,
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
        # Freeze exact result
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

            "status": (
                "ready_for_generation"
                if visual_validation.get(
                    "passed"
                )
                else "visual_design_rejected"
            ),

            "target_minutes": (
                TARGET_MINUTES
            ),

            "quality": db_quality,
        }

        # --------------------------------------------------------
        # Upload analysis result
        # --------------------------------------------------------

        result_path = (
            f"jobs/{JOB_ID}/analysis.json"
        )

        update_job(
            stage="uploading_result",
            progress=92,
        )

        print(
            f"Uploading result: {result_path}"
        )

        upload_json(
            result,
            result_path,
        )

        # --------------------------------------------------------
        # Delete original source
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
        # Final status
        # --------------------------------------------------------

        if visual_validation.get(
            "passed"
        ):

            final_stage = (
                "ready_for_generation"
            )

        else:

            final_stage = (
                "visual_design_rejected"
            )

        update_job(
            status="completed",
            stage=final_stage,
            progress=100,
            result_path=result_path,
            completed_at=None,
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