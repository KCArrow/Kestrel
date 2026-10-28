#!/bin/sh
# Assemble kestrel.py from the source parts in src/ and byte-compile it as a syntax check.
# Usage: ./build.sh            (writes ./kestrel.py)
set -e
cd "$(dirname "$0")"
PARTS="p1 p1b p1c p1d p1e p2 p2b p2c p3b p3c p3d p3e p3f p3g p3 p4 p5"
: > kestrel.py.new
for p in $PARTS; do cat "src/$p.py" >> kestrel.py.new; done
PY=$(command -v python3.11 || command -v python3)
"$PY" -m py_compile kestrel.py.new
mv kestrel.py.new kestrel.py
chmod +x kestrel.py
echo "built kestrel.py ($(wc -l < kestrel.py) lines) with $PY"
