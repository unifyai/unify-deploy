"""
Pytest configuration for unify tests.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add the project root to the Python path so tests can import modules
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))
