import os
import sys

# Make the plugin's `core` package importable in tests.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin", "javahost"))
