// Derive an emulator-compatible firebase.json from the frontend's own
// (api#301).
//
// The frontend declares `firestore` as an array of named databases
// (greenearth-stage, greenearth-prod) for deployment. The Firestore emulator
// doesn't support multiple databases and ignores that shape entirely — it
// logs "Did not find a Cloud Firestore rules file" and then defaults to
// allowing all reads and writes, so the deployed rules would silently not be
// applied locally. This collapses that array to the single database the
// emulator supports, still pointing at the frontend's own firestore.rules.
// Everything else passes through untouched.
//
// The output has to be written *beside* the original rather than tucked away
// in .firebase/: the CLI requires every referenced path to sit inside the
// config file's own directory (it rejects "../firestore.rules" as "outside
// of project directory", and an absolute functions.source as "not found").
// Keeping it there means the frontend's relative paths stay correct as-is.
//
// It is regenerated on every start and removed by `devctl down`/`nuke`.

import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const [frontendDir, outPath] = process.argv.slice(2);
if (!frontendDir || !outPath) {
  console.error("usage: derive-config.mjs <frontend-dir> <out-path>");
  process.exit(1);
}

const config = JSON.parse(
  readFileSync(resolve(frontendDir, "firebase.json"), "utf8"),
);

let firestore = config.firestore;
if (Array.isArray(firestore)) {
  firestore = firestore.find((entry) => entry && entry.rules) ?? {};
} else if (typeof firestore !== "object" || firestore === null) {
  firestore = {};
}

const derived = { ...config };

const single = {};
if (firestore.rules) single.rules = firestore.rules;
// Indices haven't moved to the frontend repo yet (frontend#42 is in
// progress); when they do, this picks them up with no change here.
if (firestore.indexes) single.indexes = firestore.indexes;

if (Object.keys(single).length > 0) {
  derived.firestore = single;
} else {
  delete derived.firestore;
}

// Hosting is a deploy-time concern and its rewrites point at deployed Cloud
// Run services; the emulator suite doesn't need it and we don't run it.
delete derived.hosting;

writeFileSync(outPath, `${JSON.stringify(derived, null, 2)}\n`);

console.log(`firebase: derived emulator config -> ${outPath}`);
console.log(`firebase:   rules:   ${derived.firestore?.rules ?? "(none)"}`);
console.log(
  `firebase:   indexes: ${derived.firestore?.indexes ?? "(none — not yet moved from the api repo)"}`,
);
