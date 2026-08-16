from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    core = root / "jupiter-core"

    if not core.is_dir():
        raise RuntimeError(
            "Private Jupiter core was not checked out."
        )

    fixture = (
        core
        / "fixtures"
        / "artifact2_final.json"
    )

    if not fixture.is_file():
        raise RuntimeError(
            f"Missing private fixture: {fixture}"
        )

    production_pipeline = (
        core
        / "app"
        / "intelligence"
        / "production_pipeline.py"
    )

    if not production_pipeline.is_file():
        raise RuntimeError(
            "Private production pipeline is missing."
        )

    sys.path.insert(
        0,
        str(core),
    )

    from app.intelligence.manim_generator import (
        compile_manim,
    )
    from app.intelligence.manim_renderer import (
        render_manim,
    )
    from app.intelligence.video_validator import (
        validate_video,
    )

    import json

    design = json.loads(
        fixture.read_text(
            encoding="utf-8"
        )
    )

    output_dir = root / "cloud_output"
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source = compile_manim(
        design
    )

    source_path = (
        output_dir
        / "cloud_test_scene.py"
    )

    source_path.write_text(
        source,
        encoding="utf-8",
    )

    output_path = (
        output_dir
        / "cloud_test.mp4"
    )

    print("JUPITER CLOUD RENDER TEST")
    print("PRIVATE CORE = PRESENT")
    print("FIXTURE = PRESENT")
    print(
        "SCENES =",
        len(
            design.get(
                "scenes",
                [],
            )
        ),
    )

    render_result = render_manim(
        source=source,
        output_path=output_path,
        quality="normal",
        timeout_seconds=1200,
    )

    print(
        "RENDER RESULT =",
        render_result,
    )

    if not render_result.get(
        "passed"
    ):
        raise RuntimeError(
            render_result.get(
                "message",
                "Manim render failed.",
            )
        )

    validation = validate_video(
        output_path
    )

    print(
        "VIDEO VALIDATION =",
        validation,
    )

    if not validation.get(
        "passed"
    ):
        raise RuntimeError(
            "Cloud-rendered video failed validation."
        )

    print("")
    print(
        "JUPITER CLOUD RENDER = SUCCESS"
    )
    print(
        "OUTPUT =",
        output_path,
    )


if __name__ == "__main__":
    main()
