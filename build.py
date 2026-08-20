from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Jupiter build entry point")
    parser.add_argument("--preview", action="store_true", help="Build a 10-second watermarked preview")
    args = parser.parse_args()
    if args.preview:
        from scripts.preview_runner import main as preview_main
        preview_main()
        return
    from scripts.shorts_runner import main as production_main
    production_main()


if __name__ == "__main__":
    main()
