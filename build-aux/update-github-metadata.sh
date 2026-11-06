#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Apply the curated GitHub About description and topics after publishing 0.6.0.
# Requires `gh auth login` with administration access to the repository.
set -eu
src=$(cd "$(dirname "$0")/.." && pwd)
repo=${GITHUB_REPOSITORY:-s11va9/network-manager-vless}
metadata="$src/docs/github-metadata.json"
description=$(python3 - "$metadata" <<'PY'
import json
import sys
from pathlib import Path
print(json.loads(Path(sys.argv[1]).read_text())["description"])
PY
)
gh repo edit "$repo" --description "$description"
python3 - "$metadata" <<'PY' | gh api --method PUT "repos/$repo/topics" --input -
import json
import sys
from pathlib import Path
print(json.dumps({"names": json.loads(Path(sys.argv[1]).read_text())["topics"]}))
PY
