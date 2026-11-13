#!/bin/zsh
set -eu
cd "${0:A:h}"
swiftc -O -target arm64-apple-macosx14.0 RepeatState.swift RepeatEngine.swift main.swift -o KeyRepeatHelper -framework AppKit -framework ApplicationServices -framework CoreGraphics
codesign --force --sign - KeyRepeatHelper
