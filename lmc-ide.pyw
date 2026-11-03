"""Double click this file (Windows) or run it with Python to open the IDE without installing anything."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from lmc.ide import main

sys.exit(main())
