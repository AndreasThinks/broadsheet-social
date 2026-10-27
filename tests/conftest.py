import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="crossposter-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["DEMO"] = "true"
os.environ["SECRET_KEY"] = "test-secret"
os.environ.setdefault("BASE_URL", "https://cross.example.com")
os.environ["FEEDS_DIR"] = str(Path(__file__).resolve().parent.parent / "feeds")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
