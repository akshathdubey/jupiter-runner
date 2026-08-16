from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    job_id = os.getenv("JOB_ID")

    if not job_id:
        raise RuntimeError("JOB_ID was not provided.")

    root = Path(__file__).resolve().parent
    core = root / "jupiter-core"

    if not core.is_dir():
        raise RuntimeError(
            "Private Jupiter core was not checked out."
        )

    required = [
        core / "app" / "intelligence" / "production_pipeline.py",
        core / "app" / "intelligence" / "manim_generator.py",
        core / "app" / "intelligence" / "media_muxer.py",
    ]

    missing = [
        str(path)
        for path in required
        if not path.is_file()
    ]

    if missing:
        raise RuntimeError(
            "Private Jupiter core is incomplete: "
            + ", ".join(missing)
        )

    print("JUPITER RUNNER = OK")
    print(f"JOB_ID = {job_id}")
    print("PRIVATE CORE = PRESENT")


if __name__ == "__main__":
    main()
