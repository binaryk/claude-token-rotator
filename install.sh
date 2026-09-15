#!/bin/sh
# ctr installer — idempotent, and deliberately does NOT install the monitor.
#
#   ./install.sh
#
# It symlinks ~/.local/bin/ctr to this checkout, creates ~/.config/ctr (0700),
# and installs the guarded ~/.zshrc block. The launchd monitor is a separate,
# explicit step: `ctr install-monitor`.
set -eu

REPO="$(cd "$(dirname "$0")" && pwd -P)"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/ctr"
# RULING 4: both names ship. Two symlinks to the same entry point, no wrapper.
ALT_LINK="$BIN_DIR/claude-rotator"
CONFIG_DIR="$HOME/.config/ctr"

if [ ! -f "$REPO/bin/ctr" ]; then
  echo "install.sh: $REPO/bin/ctr is missing — wrong checkout?" >&2
  exit 1
fi

chmod +x "$REPO/bin/ctr"

mkdir -p "$BIN_DIR"
ln -sfn "$REPO/bin/ctr" "$LINK"
echo "linked  $LINK -> $REPO/bin/ctr"
ln -sfn "$REPO/bin/ctr" "$ALT_LINK"
echo "linked  $ALT_LINK -> $REPO/bin/ctr"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
echo "created $CONFIG_DIR (0700)"

if ! "$LINK" install-shell; then
  echo "install.sh: 'ctr install-shell' failed (see the error above)." >&2
  echo "install.sh: the symlink and $CONFIG_DIR are in place; re-run it once fixed." >&2
fi

echo
echo "Next steps:"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "  1. Put ~/.local/bin on your PATH (it is not there yet):"
    echo "       echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
    ;;
esac
echo "  2. Register a token:        ctr add work        # prompts, no echo"
echo "  3. Check it:                ctr status"
echo "  4. Verify the install:      ctr doctor"
echo
echo "The background monitor is NOT installed. Turn it on when you want it:"
echo "       ctr install-monitor     # launchd, every 5 min"
echo "       ctr uninstall-monitor   # remove it again"
