"""python -m lmc prog.lmc   runs the compiler,  python -m lmc ide [folder]   opens the IDE."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "ide":
    from .ide import main
    sys.exit(main(sys.argv[2:]))

from .compiler import main
sys.exit(main())
