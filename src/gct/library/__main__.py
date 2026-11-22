import sys

from gct.library.cli import main, selected_generator

raise SystemExit(main(sys.argv[1:], generator=selected_generator))
