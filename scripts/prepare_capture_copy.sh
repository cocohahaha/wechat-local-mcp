#!/bin/zsh
set -euo pipefail

source_app="${1:-/Applications/WeChat.app}"
dest_app="${2:-$HOME/Applications/WeChatKeyCapture.app}"

if [[ ! -d "$source_app" ]]; then
  print -u2 "WeChat app not found: $source_app"
  exit 2
fi
if [[ -e "$dest_app" ]]; then
  print -u2 "Destination already exists; choose a different destination: $dest_app"
  exit 2
fi

mkdir -p "$(dirname "$dest_app")"
staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/wechat-key-capture.XXXXXX")"
entitlements_file="$(mktemp "${TMPDIR:-/tmp}/wechat-key-capture-entitlements.XXXXXX.plist")"
staged_app="$staging_dir/WeChatKeyCapture.app"
trap 'rm -rf "$staging_dir"; rm -f "$entitlements_file"' EXIT

plutil -create xml1 "$entitlements_file"
/usr/libexec/PlistBuddy \
  -c "Add :com.apple.security.get-task-allow bool true" \
  "$entitlements_file"

ditto "$source_app" "$staged_app"
codesign --force --deep --sign - --entitlements "$entitlements_file" "$staged_app"
mv "$staged_app" "$dest_app"

print "Created capture-only copy: $dest_app"
print "Quit the normal WeChat app before launching this copy."
