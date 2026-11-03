"""Run the compiler straight from the source folder without installing:  python lmcc-run.py prog.lmc"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from lmc.compiler import main

sys.exit(main())
