#!/bin/sh
set -eu
cd "$(dirname "$0")"
version=${1:-dev}
output=${2:-dist}
mkdir -p "$output"
output=$(cd "$output" && pwd)
: > "$output/SHA256SUMS"
for target in linux/amd64 darwin/arm64 windows/amd64; do
  platform=${target%/*}
  architecture=${target#*/}
  name=smartqa-host-$platform-$architecture
  if [ "$platform" = windows ]; then name=$name.exe; fi
  CGO_ENABLED=0 GOOS=$platform GOARCH=$architecture go build -trimpath -ldflags "-s -w -X main.version=$version" -o "$output/$name" .
  (cd "$output" && sha256sum "$name") >> "$output/SHA256SUMS"
done
cp install.sh "$output/install.sh"
