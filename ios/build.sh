#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
xcodegen generate
xcodebuild -project Companion.xcodeproj -scheme Companion -configuration Release \
  -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath build \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build
mkdir -p build/package/Payload
cp -R build/Build/Products/Release-iphoneos/Companion.app build/package/Payload/
(cd build/package && zip -qry ../Companion.ipa Payload)
printf 'Unsigned IPA: %s/build/Companion.ipa\n' "$PWD"
