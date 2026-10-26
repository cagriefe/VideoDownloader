"""Start the app, or the command-line interface when arguments are given."""

import sys


def main() -> None:
    if len(sys.argv) > 1:
        from .cli import main as cli_main

        sys.exit(cli_main(sys.argv[1:]))
    from .gui import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
