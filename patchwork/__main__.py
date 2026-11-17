import sys


def _entry() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-check":
        from patchwork.selfcheck import run

        sys.exit(run(sys.argv[2]))
    from patchwork.gui import main

    main()


if __name__ == "__main__":
    _entry()
