#!/usr/bin/env bash
# Builds smartqa-fixture.apk (the run's test app, one "Open escape" button) and
# smartqa-fixture-escape.apk (a test-owned package with one blank activity), CHE-2129.
# Same toolchain as ../artemis-accessibility-helper/build_apk.sh: aapt2, javac, d8, apksigner.
# Signed with that package's public debug keystore: these are test fixtures, not release builds.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
BUILD_TOOLS="$(find "$SDK/build-tools" -maxdepth 1 -mindepth 1 | sort -V | tail -n 1)"
PLATFORM="$(find "$SDK/platforms" -maxdepth 1 -mindepth 1 -name 'android-[0-9]*' | sort -V | tail -n 1)"
KEYSTORE="$DIR/../artemis-accessibility-helper/debug.keystore"

build() { # <source dir> <output apk>
    local src="$1" out="$2" work
    work="$(mktemp -d)"
    "$BUILD_TOOLS/aapt2" link -I "$PLATFORM/android.jar" --manifest "$src/AndroidManifest.xml" \
        --min-sdk-version 24 --target-sdk-version 35 --version-code 1 --version-name 1.0 \
        -o "$work/unaligned.apk"
    mkdir "$work/obj"
    javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -cp "$PLATFORM/android.jar" \
        -d "$work/obj" $(find "$src/java" -name '*.java')
    "$BUILD_TOOLS/d8" --lib "$PLATFORM/android.jar" --output "$work" --min-api 24 \
        $(find "$work/obj" -name '*.class')
    python3 -I -c 'import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "a", zipfile.ZIP_DEFLATED) as z: z.write(sys.argv[2], "classes.dex")' \
        "$work/unaligned.apk" "$work/classes.dex"
    "$BUILD_TOOLS/zipalign" -f -p 4 "$work/unaligned.apk" "$work/aligned.apk"
    "$BUILD_TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
        --ks-key-alias androiddebugkey --out "$out" "$work/aligned.apk"
    rm -rf "$work"
    echo "built $out"
}

build fixture smartqa-fixture.apk
build escape smartqa-fixture-escape.apk
sha256sum smartqa-fixture.apk smartqa-fixture-escape.apk
