#!/usr/bin/env bash
# Start the Firebase emulators from the frontend checkout (api#301).
#
# Two things stand between the frontend's firebase.json and a usable emulator
# suite, both handled here rather than by duplicating config:
#
# 1. The emulators bind loopback inside the container, which Docker can't
#    publish (published ports arrive on eth0). socat re-exposes each on
#    0.0.0.0 at port+10000.
#
# 2. The frontend declares `firestore` as an array of named databases
#    (greenearth-stage, greenearth-prod) for deployment. The Firestore
#    emulator doesn't support multiple databases and ignores that shape
#    entirely — it logs "Did not find a Cloud Firestore rules file" and then
#    defaults to allowing all reads and writes, so rules would silently not
#    apply. We derive a single-database config pointing at the frontend's own
#    firestore.rules, so the rules under test are still the deployed ones.
#
# The derived config lives in the checkout's .firebase/ directory (already
# gitignored there) and uses absolute paths, so nothing relative resolves
# against the wrong directory.
set -euo pipefail

FRONTEND_DIR=/frontend
# Written beside the original: the CLI requires referenced paths to live
# inside the config file's directory. Regenerated each start, and removed
# by `devctl down` / `devctl nuke`.
#
# The filename carries the instance name for anything but the default
# instance, because every instance bind-mounts the same frontend checkout and
# one instance's `down` must not delete the config another is running from.
DERIVED_CONFIG="$FRONTEND_DIR/${GE_DEV_FIREBASE_CONFIG:-firebase.devenv.json}"
PROJECT="${GE_DEV_FIREBASE_PROJECT:-greenearth-471522}"

node /firebase/derive-config.mjs "$FRONTEND_DIR" "$DERIVED_CONFIG"

socat TCP-LISTEN:18080,fork,reuseaddr TCP:127.0.0.1:8080 &
socat TCP-LISTEN:19099,fork,reuseaddr TCP:127.0.0.1:9099 &
socat TCP-LISTEN:15001,fork,reuseaddr TCP:127.0.0.1:5001 &
# The Emulator UI (4000) and the Firestore data viewer's websocket
# (9150) are browser-facing too, so they need the same treatment.
socat TCP-LISTEN:14000,fork,reuseaddr TCP:127.0.0.1:4000 &
socat TCP-LISTEN:19150,fork,reuseaddr TCP:127.0.0.1:9150 &

# The Functions emulator loads functions/lib (package main), so the
# TypeScript has to be compiled before the emulator starts.
cd "$FRONTEND_DIR/functions"
# npm ci, not install: install rewrites package-lock.json in the
# developer's bind-mounted checkout.
npm ci --no-audit --no-fund
npm run build
cd "$FRONTEND_DIR"

exec firebase emulators:start \
  --only auth,firestore,functions \
  --project "$PROJECT" \
  --config "$DERIVED_CONFIG"
