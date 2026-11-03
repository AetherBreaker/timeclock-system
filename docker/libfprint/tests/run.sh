#!/bin/sh
# Checks the patches here against libfprint 1.94.100 on Debian trixie (the app image's base): libfprint's virtual-image
# tests plus ours (identify_tests.py.in, spliced into its test class), on the release unpatched, where ours fail, then
# patched, where all pass. From the repository root:
#   docker run --rm -v "$PWD/docker/libfprint:/patches:ro" debian:trixie sh /patches/tests/run.sh
set -eu
apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl gcc git pkg-config meson \
  ninja-build libglib2.0-dev libgusb-dev libssl-dev libgirepository1.0-dev gobject-introspection libcairo2-dev \
  python3 python3-gi python3-gi-cairo python3-cairo gir1.2-glib-2.0 gir1.2-gusb-1.0 >/dev/null 2>&1
curl -fsSL -o /tmp/l.tar.gz https://gitlab.freedesktop.org/libfprint/libfprint/-/archive/v1.94.100/libfprint-v1.94.100.tar.gz
echo "edc90e02f330a7595ceaf37f2c6ec32ed43541347fe936d3273b0bb2524fd19c  /tmp/l.tar.gz" | sha256sum -c - >/dev/null
for variant in unpatched patched; do
  src=/tmp/$variant
  mkdir -p $src && tar -xzf /tmp/l.tar.gz -C $src --strip-components=1
  if [ $variant = patched ]; then git -C $src apply /patches/*.patch; fi
  awk 'FNR==NR {extra = extra $0 "\n"; next} /^if __name__ == .__main__.:/ {printf "%s", extra} {print}' \
    /patches/tests/identify_tests.py.in $src/tests/virtual-image.py > /tmp/vi.py && cp /tmp/vi.py $src/tests/virtual-image.py
  meson setup $src/build $src -Ddrivers=uru4000,virtual_image -Dintrospection=true -Ddoc=false -Dgtk-examples=false \
    -Dudev_rules=disabled -Dudev_hwdb=disabled >/dev/null
  ninja -C $src/build >/dev/null
  echo "===== $variant"
  meson test -C $src/build --suite virtual-driver --verbose 2>&1 | grep -E "identify: best|VirtualImage.test_.* (OK|FAIL)|^Ok:|^Fail:" || true
done
