import os
import sys

# Ensure harness directory is on sys.path for test discovery
_harness_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _harness_dir not in sys.path:
    sys.path.insert(0, _harness_dir)
