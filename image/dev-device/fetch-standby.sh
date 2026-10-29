#!/bin/sh
# Download timeclock-standby's latest release into the folder $1 as standby.tar.gz, checked against its published sum:
# the copy install-standby.sh falls back to when the installer can't download one. Both images' build.sh run it.
set -eu
REPO=AetherBreaker/timeclock-standby
tag=$(wget -qO- "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
[ -n "$tag" ] || { echo "fetch-standby: no timeclock-standby release found" >&2; exit 1; }
url=https://github.com/$REPO/releases/download/$tag/timeclock-standby-$tag.tar.gz
wget -q -O "$1/standby.tar.gz" "$url"
sum=$(wget -qO- "$url.sha256" | cut -d' ' -f1)
[ "$(sha256sum "$1/standby.tar.gz" | cut -d' ' -f1)" = "$sum" ] || { echo "fetch-standby: $tag's sum doesn't match" >&2; exit 1; }
echo "fetch-standby: baked in timeclock-standby $tag"
