'use strict';

const http = require('node:http');

const DEFAULT_PORT = 3000;
const MIN_PORT = 1024;
const MAX_PORT = 65535;

/** Escape every character outside printable ASCII in value, so a refusal message stays one log line; returns the escaped string. */
function escapeNonPrintable(value) {
  return value.replace(/[^\u0020-\u007E]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/** Resolve rawValue into a validated port, or a message explaining the refusal. */
function resolvePort(rawValue) {
  if (rawValue === undefined || rawValue.trim() === '') {
    return { ok: true, port: DEFAULT_PORT };
  }

  const value = rawValue.trim();

  const shown = escapeNonPrintable(value);

  if (!/^[0-9]+$/.test(value)) {
    return { ok: false, message: `Invalid PORT value "${shown}": must be an integer between 1024 and 65535.` };
  }

  const port = Number(value);

  if (port !== 0 && port < MIN_PORT) {
    return { ok: false, message: `Invalid PORT value "${shown}": privileged ports 1-1023 are not permitted; choose a port between 1024 and 65535.` };
  }

  if (port === 0 || port > MAX_PORT) {
    return { ok: false, message: `Invalid PORT value "${shown}": must be an integer between 1024 and 65535.` };
  }

  return { ok: true, port };
}

/** Log one line for the given method and pathname, before the request is routed (method, pathname) -> void. */
function logRequest(method, pathname) {
  console.log(`${new Date().toISOString()} ${method} ${pathname}`);
}

/** Write statusCode and body to res with a text/plain Content-Type; the only response writer (res, statusCode, body) -> void. */
function sendPlainText(res, statusCode, body) {
  res.writeHead(statusCode, { 'Content-Type': 'text/plain' });
  res.end(body);
}

/** Handle GET /hello: respond 200 with the body 'Hello world' (req, res) -> void. */
function handleHello(req, res) {
  // req is read by no handler: the route key already established the method and the
  // path, and the parameter is kept so every entry in ROUTES shares one signature.
  sendPlainText(res, 200, 'Hello world');
}

/** Handle every unmatched method and path: respond 404 with the body 'Not Found' (req, res) -> void. */
function handleNotFound(req, res) {
  sendPlainText(res, 404, 'Not Found');
}

const ROUTES = {
  'GET /hello': handleHello,
};

/** Route one request through ROUTES, falling through to the 404 handler (req, res) -> void. */
function requestListener(req, res) {
  // The query string is cut with split rather than parsed with new URL: split cannot
  // throw on a malformed request target, so request data stays a plain method and path
  // comparison and never reaches a parser.
  const pathname = req.url.split('?')[0];

  logRequest(req.method, pathname);

  const handler = ROUTES[`${req.method} ${pathname}`];

  if (handler === undefined) {
    handleNotFound(req, res);
    return;
  }

  handler(req, res);
}

/** Create the HTTP server with its listener and error handler attached; returns the server. */
function createServer() {
  const server = http.createServer(requestListener);

  server.on('error', onServerError);

  return server;
}

/** Report a server 'error' event: a listen failure exits 1 and a post-bind one does not, the port coming from error.port (error) -> void. */
function onServerError(error) {
  if (error.code === 'EADDRINUSE') {
    console.error(`Port ${error.port} is already in use. Stop the process using it or set PORT to a free port.`);
  } else if (error.syscall === 'listen') {
    console.error(`Failed to start server: ${error.message}`);
  } else {
    console.error(`Server error while serving: ${error.message}`);
    return;
  }

  process.exitCode = 1;
}

/** Shut the given server down on SIGINT, closing its connections and exiting with status 0. */
function shutdown(server) {
  console.log('Received SIGINT. Shutting down gracefully.');

  // The two connection-closing calls below are typeof-guarded because
  // closeIdleConnections() and closeAllConnections() were added in Node v18.2.0, above
  // the >=18.0.0 floor this program declares; the failsafe timer still guarantees a
  // clean exit on Node 18.0.x. Idle connections are closed because Node 18's close()
  // keeps existing connections, so an idle keep-alive socket would hold the process open.
  const failsafe = setTimeout(() => {
    // The exit waits for the line below to reach stdout instead of following it at
    // once: stdout is a pipe in any scripted run, where the write is asynchronous, and
    // process.exit() discards whatever it has not flushed. A consumer that has gone
    // away reports the failure to the error handler rather than throwing, so the
    // process still ends with status 0.
    process.stdout.once('error', () => process.exit(0));
    process.stdout.write('Forcing shutdown after 5s.\n', () => process.exit(0));

    if (typeof server.closeAllConnections === 'function') {
      server.closeAllConnections();
    }
  }, 5000);

  if (typeof server.closeIdleConnections === 'function') {
    server.closeIdleConnections();
  }

  server.close(() => {
    clearTimeout(failsafe);

    console.log('Server closed. Exiting.');

    // process.exit(0) is deliberately not called here: it truncates pending stdout
    // writes, and the line above is the evidence that the shutdown was clean.
    process.exitCode = 0;
  });
}

/** Resolve the port from the environment, create the server and start listening () -> void. */
function start() {
  const resolved = resolvePort(process.env.PORT);

  if (!resolved.ok) {
    console.error(resolved.message);
    process.exitCode = 1;
    return;
  }

  const server = createServer();

  process.on('SIGINT', () => shutdown(server));

  server.listen(resolved.port, () => {
    console.log(`Server listening on port ${resolved.port}`);
  });
}

start();
