#!/usr/bin/env bash
# Build a release APK with your Entra Application (client) ID baked in.
# Usage: ./tool/build_release.sh xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${1:-}" ]] || [[ "$1" != *-* ]]; then
  echo "Usage: $0 <AZURE_CLIENT_ID>"
  echo "Example: $0 12345678-1234-1234-1234-123456789abc"
  echo ""
  echo "Get the ID from Azure Portal → App registrations → your app → Overview."
  exit 1
fi
export FLUTTER_NO_ANALYTICS="${FLUTTER_NO_ANALYTICS:-1}"
flutter pub get
flutter build apk --release --dart-define=AZURE_CLIENT_ID="$1"
echo ""
echo "APK: $(pwd)/build/app/outputs/flutter-apk/app-release.apk"
