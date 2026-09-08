import assert from "node:assert/strict";
import test from "node:test";

import { rewriteEmulatorConfig } from "./ui-proxy.mjs";

test("rewrites browser-facing ports for a named instance", () => {
  const config = {
    projectId: "greenearth-471522",
    ui: {
      host: "127.0.0.1",
      port: 4000,
      listen: [
        { address: "127.0.0.1", family: "IPv4", port: 4000 },
        { address: "::1", family: "IPv6", port: 4000 },
      ],
    },
    firestore: {
      host: "127.0.0.1",
      port: 8080,
      listen: [{ address: "127.0.0.1", family: "IPv4", port: 8080 }],
      webSocketHost: "127.0.0.1",
      webSocketPort: 9150,
    },
    auth: { host: "127.0.0.1", port: 9099 },
    functions: { host: "127.0.0.1", port: 5001 },
    hub: { host: "127.0.0.1", port: 4400 },
  };

  const rewritten = rewriteEmulatorConfig(config, {
    host: "localhost",
    ui: 4010,
    firestore: 8090,
    firestoreWebSocket: 9160,
    auth: 9109,
    functions: 5011,
  });

  assert.equal(rewritten.ui.host, "localhost");
  assert.equal(rewritten.ui.port, 4010);
  assert.deepEqual(
    rewritten.ui.listen.map((listener) => listener.port),
    [4010, 4010],
  );
  assert.equal(rewritten.firestore.host, "localhost");
  assert.equal(rewritten.firestore.port, 8090);
  assert.equal(rewritten.firestore.listen[0].port, 8090);
  assert.equal(rewritten.firestore.webSocketHost, "localhost");
  assert.equal(rewritten.firestore.webSocketPort, 9160);
  assert.equal(rewritten.auth.port, 9109);
  assert.equal(rewritten.functions.port, 5011);

  // Hub traffic stays behind the UI server and is not exposed on the host.
  assert.equal(rewritten.hub.port, 4400);
});

test("tolerates optional emulators missing from the config", () => {
  const config = { projectId: "greenearth-471522" };

  assert.deepEqual(
    rewriteEmulatorConfig(config, {
      host: "127.0.0.1",
      ui: 4000,
      firestore: 8080,
      firestoreWebSocket: 9150,
      auth: 9099,
      functions: 5001,
    }),
    config,
  );
});
