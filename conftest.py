"""
Root conftest.py — sets DB_PATH to a temp test database BEFORE any
server module is imported.  Server modules create AccountsDB/ProductsDB/
ComplianceDB at module level, so the env var must be in place first.
"""

import os
import tempfile

# Create a temp file for the test DB; set it before any server import.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TEST_DB_PATH = _tmp.name
_tmp.close()

os.environ["DB_PATH"] = _TEST_DB_PATH

# Seed the test DB now so every test that imports a server module gets data.
from shared.database import init_db, seed_db
init_db(_TEST_DB_PATH)
seed_db(_TEST_DB_PATH)
