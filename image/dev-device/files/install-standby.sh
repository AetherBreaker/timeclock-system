#!/bin/sh
# Run by late.sh in the installed system: the standby screen, screen watcher and boot splash (timeclock-standby,
# standby-screen spec §9), from its latest release when the network allows, else from the copy baked in at build time.
# Its install.sh does the rest, and starts nothing in the installer's chroot.
set -eu
S=$(dirname "$0")
REPO=AetherBreaker/timeclock-standby
work=$(mktemp -d)
tarball=$S/standby.tar.gz
if tag=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1) &&
  [ -n "$tag" ]; then
  url=https://github.com/$REPO/releases/download/$tag/timeclock-standby-$tag.tar.gz
  if curl -fsSL -o "$work/latest.tar.gz" "$url" && sum=$(curl -fsSL "$url.sha256" | cut -d' ' -f1) &&
    [ "$(sha256sum "$work/latest.tar.gz" | cut -d' ' -f1)" = "$sum" ]; then
    tarball=$work/latest.tar.gz
  fi
fi
echo "install-standby: from $tarball"
tar -xzf "$tarball" -C "$work"
sh "$work"/timeclock-standby-*/install.sh
rm -rf "$work"
