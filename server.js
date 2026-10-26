'use strict';

const http = require('node:http');

const DEFAULT_PORT = 3000;
const MIN_PORT = 1024;
const MAX_PORT = 65535;

const ROUTES = {
  'GET /hello': handleHello,
};

/** Resolve rawValue into a validated port, or a message explaining the refusal. */
function resolvePort(rawValue) {
  if (rawValue === undefined || rawValue.trim() === '') {
    return { ok: true, port: DEFAULT_PORT };
  }

  const value = rawValue.trim();

  if (!/^[0-9]+$/.test(value)) {
    return { ok: false, message: `Invalid PORT value "${value}": must be an integer between 1024 and 65535.` };
  }

  const port = Number(value);

  if (port !== 0 && port < MIN_PORT) {
    return { ok: false, message: `Invalid PORT value "${value}": privileged ports 1-1023 are not permitted; choose a port between 1024 and 65535.` };
  }

  if (port === 0 || port > MAX_PORT) {
    return { ok: false, message: `Invalid PORT value "${value}": must be an integer between 1024 and 65535.` };
  }

  return { ok: true, port };
}

/** Log one line for the given method and pathname, before the request is routed. */
function logRequest(method, pathname) {
  console.log(`${new Date().toISOString()} ${method} ${pathname}`);
}

/** Write statusCode and body to res with a text/plain Content-Type; the only response writer. */
function sendPlainText(res, statusCode, body) {
  res.writeHead(statusCode, { 'Content-Type': 'text/plain' });
  res.end(body);
}

/** Handle GET /hello: respond 200 with the body 'Hello world'. */
function handleHello(req, res) {
  // req is read by no handler: the route key already established the method and the
  // path, and the parameter is kept so every entry in ROUTES shares one signature.
  sendPlainText(res, 200, 'Hello world');
}

/** Handle every unmatched method and path: respond 404 with the body 'Not Found'. */
function handleNotFound(req, res) {
  sendPlainText(res, 404, 'Not Found');
}

/** Route one request: derive the pathname, log it, dispatch on ROUTES, then fall through to 404. */
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

/** Create the HTTP server with its request listener and its error handler attached. */
function createServer() {
  const server = http.createServer(requestListener);

  server.on('error', onServerError);

  return server;
}

/** Report a bind failure on stderr and set exit code 1; the failed port comes from error.port. */
function onServerError(error) {
  if (error.code === 'EADDRINUSE') {
    console.error(`Port ${error.port} is already in use. Stop the process using it or set PORT to a free port.`);
  } else {
    console.error(`Failed to start server: ${error.message}`);
  }

  process.exitCode = 1;
}

/** Shut the server down on SIGINT, closing its connections and exiting with status 0. */
function shutdown(server) {
  console.log('Received SIGINT. Shutting down gracefully.');

  // The two connection-closing calls below are typeof-guarded because
  // closeIdleConnections() and closeAllConnections() were added in Node v18.2.0, above
  // the >=18.0.0 floor this program declares; the failsafe timer still guarantees a
  // clean exit on Node 18.0.x. Idle connections are closed because Node 18's close()
  // keeps existing connections, so an idle keep-alive socket would hold the process open.
  const failsafe = setTimeout(() => {
    console.log('Forcing shutdown after 5s.');

    if (typeof server.closeAllConnections === 'function') {
      server.closeAllConnections();
    }

    process.exit(0);
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

/** Resolve the port from the environment, create the server and start listening. */
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
