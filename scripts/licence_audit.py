"""Print licence facts for every installed distribution (from package metadata).

Run inside the project venv:  .venv/bin/python -I scripts/licence_audit.py
Review by hand: metadata is what the package *claims*; bundled binaries (e.g. pysdl2-dll)
carry their own licences under dist-info/licenses/.
"""

import re
from importlib.metadata import distributions

for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
    meta = dist.metadata
    expr = meta.get("License-Expression") or meta.get("License") or ""
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License")]
    files = [str(f) for f in dist.files or [] if re.search(r"(LICENSE|COPYING)", str(f), re.IGNORECASE)]
    print(f"{meta['Name']} {meta['Version']} | {expr[:70]!r} | {classifiers} | {len(files)} licence file(s)")
