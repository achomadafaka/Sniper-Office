#!/bin/sh
# Copy the local Hermes install into the seat bridge, read-only, for Hermes seats (sandbox path /opt/hermes). Run again after a Hermes update.
# Usage: scripts/publish_hermes.sh [bridge=/srv/sniper-office]
set -eu
B=${1:-/srv/sniper-office}/hermes
PY=$(ls -d "$HOME"/.hermes/tools/python-3.14*-linux-x64 | tail -1)
SITE=$("$PY/bin/python3" -I -c "import sys;sys.path.insert(0,'$HOME/.hermes/hermes-agent');import hermes_bootstrap,openai,os;print(os.path.dirname(os.path.dirname(openai.__file__)))")
mkdir -p "$B"
rsync -a --delete "$PY/" "$B/python/"
rsync -a --delete "$SITE/" "$B/site/"
rsync -a --delete --exclude=node_modules --exclude=.git --exclude=website --exclude=web --exclude=ui-tui --exclude=tests --exclude=tests-js --exclude=apps --exclude=__pycache__ "$HOME/.hermes/hermes-agent/" "$B/agent/"
# Hermes relaunches python for its gateway; a .pth keeps the agent and its packages importable there too, even under -I.
printf '/opt/hermes/agent\n/opt/hermes/site\n' > "$B/python/lib/python3.14/site-packages/office-hermes.pth"
chmod -R a+rX,go-w "$B"
echo "published $(du -sh "$B" | cut -f1) to $B"
