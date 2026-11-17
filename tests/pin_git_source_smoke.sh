#!/usr/bin/env bash
# Offline smoke test for pin_git_source (no network, no container).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$SELF_DIR/engine/common.sh"

# Extract the function from common.sh (between "pin_git_source() {" and the first "^}")
awk '/^pin_git_source\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SRC" > /tmp/pin_fn.sh
bash -n /tmp/pin_fn.sh
# shellcheck disable=SC1091
source /tmp/pin_fn.sh

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# "upstream" repo with two commits; the second one touches csrc/
git init -q "$tmp/up" && git -C "$tmp/up" config user.email t@t && git -C "$tmp/up" config user.name t
mkdir -p "$tmp/up/csrc" && echo a > "$tmp/up/a.txt" && echo c > "$tmp/up/csrc/k.cc"
git -C "$tmp/up" add -A && git -C "$tmp/up" commit -qm base
git -C "$tmp/up" branch -q base
echo b > "$tmp/up/a.txt" && echo d > "$tmp/up/csrc/k.cc"
git -C "$tmp/up" add -A && git -C "$tmp/up" commit -qm work
git -C "$tmp/up" branch -q work

# "image" clone: shallow, HEAD is the built commit (like /vllm-workspace/vllm)
git clone -q --depth=1 --branch base "file://$tmp/up" "$tmp/img"

echo "--- image HEAD: $(git -C "$tmp/img" rev-parse --short HEAD) shallow=$(git -C "$tmp/img" rev-parse --is-shallow-repository)"

echo "--- case 1: branch only touches python"
git -C "$tmp/up" checkout -q -b pyonly base
echo e > "$tmp/up/py.txt" && git -C "$tmp/up" add -A && git -C "$tmp/up" commit -qm py
pin_git_source test "$tmp/img" "$tmp/up" pyonly "" 0
echo "    head now: $(git -C "$tmp/img" rev-parse --short HEAD) (want $(git -C "$tmp/up" rev-parse --short pyonly))"

echo "--- case 2: branch touches csrc (expect WARN)"
pin_git_source test "$tmp/img" "$tmp/up" work "" 0 csrc
echo "    head now: $(git -C "$tmp/img" rev-parse --short HEAD) (want $(git -C "$tmp/up" rev-parse --short work))"

echo "--- case 3: bogus branch, not required (expect WARN + keep HEAD)"
pin_git_source test "$tmp/img" "$tmp/up" no-such-branch "" 0
echo "    head still: $(git -C "$tmp/img" rev-parse --short HEAD)"

echo "--- case 4: bogus branch, required (expect non-zero exit)"
if (pin_git_source test "$tmp/img" "$tmp/up" no-such-branch "" 1); then
  echo "    UNEXPECTED success"
  exit 1
else
  echo "    refused to start, as expected"
fi
echo OK
