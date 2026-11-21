"""Build step: download sqlglot at the pinned commit from GitHub into /app/sqlglot."""

import io
import os
import shutil
import sys
import tarfile
import urllib.request

sha = sys.argv[1]
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
for attempt in range(5):
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
        break
    except Exception as e:  # retry transient network errors
        print(f"download failed ({e}); retrying", file=sys.stderr)
else:
    raise SystemExit("could not download sqlglot")
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt", filter="data")
os.makedirs("/app", exist_ok=True)
shutil.move(f"/opt/sqlglot-{sha}", "/app/sqlglot")
