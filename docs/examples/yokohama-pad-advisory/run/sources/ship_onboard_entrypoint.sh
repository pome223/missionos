#!/bin/sh
# The stock PX4 headless option suppresses the GUI but does not enable EGL.
# Modify only this disposable container's launch script, then use stock PX4.
set -eu
python3 - <<'PY'
from pathlib import Path
p = Path('/opt/px4-gazebo/etc/init.d-posix/px4-rc.gzsim')
s = p.read_text()
needle = ' -r -s '
if s.count(needle) != 1:
    raise RuntimeError('Unknown PX4 Gazebo launcher; refusing an unverified edit')
p.write_text(s.replace(needle, ' --headless-rendering -r -s '))
PY
exec /opt/px4-gazebo/bin/px4-entrypoint.sh "$@"
