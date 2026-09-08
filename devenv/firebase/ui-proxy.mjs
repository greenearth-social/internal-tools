#!/usr/bin/env node

/**
 * Proxy the Firebase Emulator UI and advertise browser-reachable ports.
 *
 * firebase-tools binds its services to fixed loopback ports inside the
 * container and returns those same ports from /api/config. That is correct for
 * the default devenv, but a named instance publishes an offset block of host
 * ports. The UI runs in the developer's browser, so handing it container ports
 * makes the Firestore/Auth/Functions pages connect to the wrong instance (or
 * to nothing at all).
 *
 * Normal UI traffic is streamed unchanged. Only /api/config is buffered and
 * rewritten with the host ports allocated by devctl.
 */

import http from "node:http";
import net from "node:net";
import { pathToFileURL } from "node:url";

const UPSTREAM_HOST = process.env.GE_DEV_FIREBASE_UI_UPSTREAM_HOST || "127.0.0.1";
const UPSTREAM_PORT = envPort("GE_DEV_FIREBASE_UI_UPSTREAM_PORT", 4000);
const PROXY_PORT = envPort("GE_DEV_FIREBASE_UI_PROXY_PORT", 14000);

function envPort(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${name} must be an integer from 1 to 65535 (got ${JSON.stringify(raw)})`);
  }
  return port;
}

function browserPortsFromEnv() {
  return {
    host: process.env.GE_DEV_EMULATOR_HOST || "127.0.0.1",
    ui: envPort("GE_DEV_PORT_FIREBASE_UI", 4000),
    firestore: envPort("GE_DEV_PORT_FIRESTORE", 8080),
    firestoreWebSocket: envPort("GE_DEV_PORT_FIREBASE_UI_WS", 9150),
    auth: envPort("GE_DEV_PORT_FIREBASE_AUTH", 9099),
    functions: envPort("GE_DEV_PORT_FUNCTIONS", 5001),
  };
}

function rewriteEndpoint(endpoint, host, port) {
  if (!endpoint || typeof endpoint !== "object") return;
  endpoint.host = host;
  endpoint.port = port;
  if (Array.isArray(endpoint.listen)) {
    for (const listener of endpoint.listen) {
      if (listener && typeof listener === "object") listener.port = port;
    }
  }
}

export function rewriteEmulatorConfig(config, ports) {
  rewriteEndpoint(config.ui, ports.host, ports.ui);
  rewriteEndpoint(config.firestore, ports.host, ports.firestore);
  rewriteEndpoint(config.auth, ports.host, ports.auth);
  rewriteEndpoint(config.functions, ports.host, ports.functions);

  if (config.firestore && typeof config.firestore === "object") {
    config.firestore.webSocketHost = ports.host;
    config.firestore.webSocketPort = ports.firestoreWebSocket;
  }
  return config;
}

function proxyHeaders(req, rewriteConfig) {
  const headers = { ...req.headers, host: `${UPSTREAM_HOST}:${UPSTREAM_PORT}` };
  if (rewriteConfig) headers["accept-encoding"] = "identity";
  return headers;
}

function writeRewrittenConfig(upstreamResponse, response, chunks) {
  const original = Buffer.concat(chunks);
  if ((upstreamResponse.statusCode || 500) >= 300) {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    response.end(original);
    return;
  }

  let rewritten;
  try {
    const config = JSON.parse(original.toString("utf8"));
    rewritten = Buffer.from(
      `${JSON.stringify(rewriteEmulatorConfig(config, browserPortsFromEnv()), null, 2)}\n`,
    );
  } catch (error) {
    console.error(`firebase-ui-proxy: could not rewrite /api/config: ${error}`);
    response.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
    response.end("Firebase UI returned an invalid emulator config\n");
    return;
  }

  const headers = { ...upstreamResponse.headers };
  delete headers["content-encoding"];
  delete headers.etag;
  headers["content-type"] = "application/json; charset=utf-8";
  headers["content-length"] = String(rewritten.length);
  response.writeHead(upstreamResponse.statusCode || 200, headers);
  response.end(rewritten);
}

export function createUiProxy() {
  const server = http.createServer((request, response) => {
    const rewriteConfig = new URL(request.url || "/", "http://firebase-ui").pathname === "/api/config";
    const upstreamRequest = http.request(
      {
        hostname: UPSTREAM_HOST,
        port: UPSTREAM_PORT,
        method: request.method,
        path: request.url,
        headers: proxyHeaders(request, rewriteConfig),
      },
      (upstreamResponse) => {
        if (!rewriteConfig) {
          response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
          upstreamResponse.pipe(response);
          return;
        }

        const chunks = [];
        upstreamResponse.on("data", (chunk) => chunks.push(chunk));
        upstreamResponse.on("end", () => writeRewrittenConfig(upstreamResponse, response, chunks));
      },
    );

    upstreamRequest.on("error", (error) => {
      if (response.headersSent) {
        response.destroy(error);
        return;
      }
      response.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
      response.end(`Firebase Emulator UI is not ready: ${error.message}\n`);
    });
    request.pipe(upstreamRequest);
  });

  // The current Firebase UI opens the Firestore websocket directly on the
  // advertised websocket port, but transparently forwarding upgrades here
  // keeps the proxy equivalent to the old raw TCP bridge for other UI traffic.
  server.on("upgrade", (request, socket, head) => {
    const upstream = net.connect(UPSTREAM_PORT, UPSTREAM_HOST, () => {
      let prelude = `${request.method} ${request.url} HTTP/${request.httpVersion}\r\n`;
      for (let index = 0; index < request.rawHeaders.length; index += 2) {
        prelude += `${request.rawHeaders[index]}: ${request.rawHeaders[index + 1]}\r\n`;
      }
      upstream.write(`${prelude}\r\n`);
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on("error", () => socket.destroy());
  });

  return server;
}

function main() {
  const server = createUiProxy();
  server.listen(PROXY_PORT, "0.0.0.0", () => {
    console.log(
      `firebase-ui-proxy: 0.0.0.0:${PROXY_PORT} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}`,
    );
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
