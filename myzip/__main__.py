"""인자 없이 실행하면 GUI, 명령(c/x/l/t)을 주면 CLI, 파일 경로만 주면 GUI로 열기."""
import sys


def run():
    args = sys.argv[1:]
    if args and args[0] in ("c", "x", "l", "t", "-h", "--help", "--version"):
        from .cli import main
        sys.exit(main(args))
    from .gui import main
    main(args[0] if args else None)


if __name__ == "__main__":
    run()
