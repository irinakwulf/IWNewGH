# Node.js Hello World HTTP Server Tutorial

A minimal, dependency-free Node.js HTTP server you can read end to end: three files at the repository root — `server.js`, `package.json` and `README.md` — one working endpoint, one explicit 404 path, one log line per request, one configurable port, and a shutdown that leaves nothing behind.

This is teaching material that also runs correctly, at tutorial scale. Every response, log line and exit status documented here is what the delivered code produces, and it is not a production service — the deliberate boundaries are listed under Scope and non-goals below.

## Purpose

`server.js` is a single-file reference implementation of an HTTP server built on Node's built-in `http` module: no framework, no middleware, no routing library and no third-party package of any kind. The whole program is about 150 lines and reads top to bottom.

Reading it, you see each of these in isolation:

- creating one server instance with `http.createServer(requestListener)`;
- handling an incoming request and writing a response;
- matching the request method and the request path against a routing table;
- logging every request before it is routed;
- resolving the listening port from an environment variable and validating it;
- shutting down gracefully when the process receives `SIGINT` (`CTRL+C`).

It is written for beginner-level Node.js developers who need a reference implementation of a server without framework dependencies, and for anyone who wants a small, correct baseline to compare against. The entire functional surface is two responses and one console line per request, which is what makes it readable in one sitting.

It is teaching material, and it runs correctly. It is deliberately at tutorial scale rather than a production service — one anonymous read-only endpoint, no persistence, no authentication, nothing that protects it against real traffic — and the boundaries are enumerated under Scope and non-goals.

The extension point is the `ROUTES` table in `server.js`: adding an endpoint is one entry in that table plus one handler function, and nothing else in the program changes — not the logging, not the response writer, not the configuration or the shutdown code.

## Prerequisites

- **Node.js 18.0.0 or later.** `package.json` declares `"engines": { "node": ">=18.0.0" }`, and every API the program uses is available at 18.0.0. The two connection-closing calls used during shutdown were added in Node 18.2.0, so the code detects them before calling them and still exits cleanly on 18.0.x.
- **`curl`** for the requests documented in this README.

Check the runtime:

```bash
node --version
```

Expected: `v18.0.0` or higher. Any later LTS release also satisfies the floor.

`seq` and `ps` are used only by the optional performance and memory checks (V7, V8 and V9 under Verification). A POSIX shell is needed throughout: the `PORT=… node server.js` form, the scripted shutdown under Stopping the server, and the checks that drive the server from the shell — V5's counting block, V6, V8, V9, V11, V12 and V13 — all rely on your shell's own job control or `$?`. Nothing else is needed. There is no build tool, no database and no package to install.

## Installation

There is no installation step.

`package.json` declares no `dependencies` and no `devDependencies`, because the program uses only Node's built-in modules. So:

- `npm install` is unnecessary and is not part of any step in this README;
- `node_modules` is never created;
- `package-lock.json` does not exist.

Copy or clone the three files — `server.js`, `package.json` and `README.md` — and you are ready to run the server.

## Running the server

From the repository root:

```bash
node server.js
```

or, equivalently, through the manifest's start script:

```bash
npm start
```

`scripts.start` is `node server.js`, so both commands run the same program. `npm start` prints npm's own banner lines before the server's output.

Expected output:

```
Server listening on port 3000
```

That line is written once the port is actually bound: `server.listen()` is asynchronous, so the confirmation comes from the listen callback rather than from the statement after the call. The process then stays in the foreground and serves requests until you stop it.

The server binds the port alone, with no host argument (`server.listen(port)`), which makes it reachable at `http://localhost:3000`. Because no host is passed, that bind covers every interface rather than only the loopback one: while the server runs, any machine that can reach this host can reach the endpoint too, over plain HTTP, with no authentication, no access control and no rate limiting (see Scope and non-goals).

## Verifying the endpoints

With the server running in one terminal, run these in another. This is the testing instruction for the tutorial:

```bash
curl http://localhost:3000/hello
```

Expected output — the body, written with no trailing newline, so your shell prompt returns immediately after `world`:

```
Hello world
```

Add `-s -i` to see the status line and the headers:

```bash
curl -si http://localhost:3000/hello
```

Expected output:

```
HTTP/1.1 200 OK
Content-Type: text/plain
Date: Mon, 26 Oct 2026 07:56:28 GMT
Connection: keep-alive
Keep-Alive: timeout=5
Transfer-Encoding: chunked

Hello world
```

The contract is the status line, the `Content-Type` header and the body: `200 OK`, `text/plain`, `Hello world` — 11 bytes, no trailing newline. The `Date` value changes on every response, and `Date`, `Connection`, `Keep-Alive` and the framing header are added by the runtime rather than by the program (see Runtime-added headers below).

The same contract, measured in one line:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' http://localhost:3000/hello
```

Expected output:

```
200 text/plain 11
```

### The failure contract

Every request that is not exactly `GET /hello` gets the catch-all response:

```bash
curl -si http://localhost:3000/invalid
```

Expected output:

```
HTTP/1.1 404 Not Found
Content-Type: text/plain
Date: Mon, 26 Oct 2026 07:56:28 GMT
Connection: keep-alive
Keep-Alive: timeout=5
Transfer-Encoding: chunked

Not Found
```

Only the status line and the body differ from the 200 response. The body is exactly `Not Found` — 9 bytes, no trailing newline — and it is a fixed literal: no requested path is echoed back, no error code, no stack trace and no runtime detail appears in it.

Measured in one line:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' http://localhost:3000/invalid
```

Expected output:

```
404 text/plain 9
```

### A method that is not matched

```bash
curl -si -X POST http://localhost:3000/hello
```

Expected output: the same 404 contract as above, with the body `Not Found`. A method mismatch is not a `405` and not a `200`; it is the catch-all. `curl -X POST` without `-d` sends no request body and adds no `Content-Length` header, so nothing is left unread on the connection.

### Matching semantics

The comparison is exact and case-sensitive, on the query-stripped path and on the method:

| Request | Response |
|---------|----------|
| `GET /hello` | `200`, body `Hello world` |
| `GET /hello?anything=1` | `200`, body `Hello world` (the query string is removed before comparison) |
| `GET /hello/` | `404`, body `Not Found` — no trailing-slash tolerance |
| `GET /Hello`, `GET /HELLO` | `404`, body `Not Found` — the path is case-sensitive |
| `GET /hello%2F` | `404`, body `Not Found` — no URL decoding is performed |
| `POST /hello`, `PUT /hello` | `404`, body `Not Found` — the method must match exactly |
| `HEAD /hello` | `404`, no body on the wire — the method must match exactly, and `HEAD` is not treated as `GET` |
| `GET /` or any other path | `404`, body `Not Found` |

The handler writes the same `Not Found` literal in every unmatched case, but HTTP forbids a body on a `HEAD` response and the runtime omits it, so a `HEAD` probe observes the status and the `Content-Type` only — 0 bytes — while the same request sent as `GET` observes the 9-byte body.

Every non-matching request receives the identical response, which does not reveal which check failed.

### Runtime-added headers

Node adds its own headers to every response: `Date`, `Connection` (with `Keep-Alive` on a kept-alive connection) and the framing header it chooses — `Transfer-Encoding: chunked` on this runtime, or `Content-Length` where the runtime sets it. None of them is part of the contract, which is the status code, `Content-Type: text/plain` and the byte-exact body.

## Request logging

Every request produces exactly one line on stdout, written before the request is routed — so a request that ends in 404 is logged just like one that succeeds. The format is three space-separated tokens, and each `<...>` below is the substitution for that token rather than text that appears in the output:

```
<ISO-8601 timestamp> <HTTP method> <pathname>
```

Literal examples from one session, the first for the request above and the second for the rejected one:

```
2026-10-26T07:56:28.326Z GET /hello
2026-10-26T07:56:28.334Z GET /invalid
```

- The timestamp is `Date.prototype.toISOString()`: UTC, milliseconds, and a `Z` suffix, so it always matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$`.
- The method is the method as received, exactly as sent by the client.
- The third token is the query-stripped path — the same value the router matches on — so `GET /hello?x=1` logs `2026-10-26T07:56:28.326Z GET /hello`.

Operational output — the startup line, every request line and the shutdown lines — goes to stdout. Diagnostics go to stderr, so the two streams can be separated without parsing prefixes.

## Stopping the server

Press `CTRL+C` in the terminal running the server. That sends `SIGINT`, and the server stops accepting new connections, lets in-flight responses finish, closes its idle connections and exits:

```
Received SIGINT. Shutting down gracefully.
Server closed. Exiting.
```

The exit status is `0`.

The same thing, scripted — background the server, signal it, then read its status:

```bash
node server.js & pid=$!; sleep 1; kill -INT $pid; wait $pid; echo "exit=$?"
```

Expected output:

```
Server listening on port 3000
Received SIGINT. Shutting down gracefully.
Server closed. Exiting.
exit=0
```

The success path deliberately does not call `process.exit()`: an early process exit truncates pending stdout writes, and `Server closed. Exiting.` is itself the evidence that the shutdown was clean. The program sets `process.exitCode = 0` and lets the process end naturally once no handle remains.

If a connection refuses to finish within five seconds, a single-shot failsafe closes the remaining connections and prints `Forcing shutdown after 5s.` You will not see that line on a normal `CTRL+C`.

## Configuration

There is exactly one environment variable: `PORT`.

| Setting | Value |
|---------|-------|
| Default | `3000`, used when `PORT` is unset or empty |
| Accepted range | `1024`-`65535` |
| Read | once, at startup |

Override the default:

```bash
PORT=4000 node server.js
```

Expected output:

```
Server listening on port 4000
```

The endpoint then answers on 4000 — `curl -si http://localhost:4000/hello` returns the 200 contract above — and the startup line names the port that was resolved, not the default.

Surrounding whitespace is trimmed, so `PORT=" 4000 "` is accepted as 4000.

### When the port is refused

Four conditions stop the server before it serves anything. Each writes to stderr and exits with status `1`. In the messages below, `<value>` stands for the offending value, `<port>` for the port that could not be bound and `<error message>` for the runtime's own description of the failure: the program substitutes the actual text, so the messages it writes contain no angle brackets — V11, V12 and V13 under Verification show three of them with a value in place.

**Not a decimal integer, or out of range.** This covers `abc`, `3000.5`, `-1`, `0x1F90`, `1e3`, `+3000`, `65536` and `0`:

```
Invalid PORT value "<value>": must be an integer between 1024 and 65535.
```

Only digits are accepted, so the values above are refused rather than reinterpreted: `0x1F90` is not read as 8080, `1e3` is not read as 1000, `+3000` is not read as 3000, and `0` is out of range rather than privileged.

**A privileged port, 1-1023:**

```
Invalid PORT value "<value>": privileged ports 1-1023 are not permitted; choose a port between 1024 and 65535.
```

**The port is already held by another process:**

```
Port <port> is already in use. Stop the process using it or set PORT to a free port.
```

**Any other failure to bind.** A listen failure whose cause is not the port being held is reported with the runtime's own message for it:

```
Failed to start server: <error message>
```

It exits with status `1` like the others, and no gate under Verification poses it: V13 covers the port-in-use case, and inducing a different listen failure depends on the host.

Validation happens before `listen()` is called, which is why the privileged-port refusal needs no root privileges and produces no permission error: it is a validation outcome, not a failed bind. There is no override variable for privileged ports — the `1024` floor is the explicit configuration, and no second environment variable exists.

### Exit statuses

`0` for a clean `SIGINT` shutdown, and `1` for invalid configuration, a privileged port and any bind failure. Nothing else terminates the process.

## Verification

These are the checks that prove the behaviour described above. Run them from the repository root with the server started as documented — the gates use the default port `3000` unless a gate says otherwise — and compare the output literally. V7, V8 and V9 are measurements taken with host tools; every other gate is an exact match.

The gates split into three groups by what they expect of port `3000`. V2, V3, V4 — and the in-terminal half of V5 — issue requests only, so they expect the server you started under Running the server above to still be listening on `3000`. V1, V5's counting block, V6, V9, V13 and V14's `npm start` each start a server of their own on `3000`, so stop that one with `CTRL+C` before you run any of them: against an already-held port each prints `Port 3000 is already in use. Stop the process using it or set PORT to a free port.` on stderr and exits `1` instead of producing the output shown below. V7 and V8 issue requests only too, but they come after those self-starting gates, so start a server again before them — `node server.js` in a second terminal, stopped with `CTRL+C` when you are finished — and V8's 50 request lines then appear on that server's stdout. V10 starts its own server on `4000`, V11 and V12 refuse a value before any bind is attempted, and V16 binds nothing, so none of those needs `3000` to be free.

**V1 — starts on the default port**

```bash
node server.js
```

Expected stdout, exactly once:

```
Server listening on port 3000
```

stderr is empty, and the process stays in the foreground serving requests until you stop it.

**V2 — `GET /hello`**

```bash
curl -si http://localhost:3000/hello
curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' http://localhost:3000/hello
```

Expected: the status line `HTTP/1.1 200 OK`, the header `Content-Type: text/plain`, and the body `Hello world`; the one-line check prints

```
200 text/plain 11
```

so the status is 200, the content type is exactly `text/plain` with no `; charset=utf-8` parameter, and the body is 11 bytes with no trailing newline.

**V3 — unknown path**

```bash
curl -si http://localhost:3000/invalid
curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' http://localhost:3000/invalid
```

Expected: the status line `HTTP/1.1 404 Not Found`, the header `Content-Type: text/plain`, and the body `Not Found`; the one-line check prints

```
404 text/plain 9
```

The body is 9 bytes and contains no path echo, no error detail and no stack trace.

**V4 — method not matched**

```bash
curl -si -X POST http://localhost:3000/hello
```

Expected: `HTTP/1.1 404 Not Found` with the body `Not Found` — the identical catch-all contract V3 produces, and neither a 405 nor a 200.

**V5 — request logging**

With the server in one terminal, issue V2 and V3 from another, then read the server's stdout. Expected: two lines, in order, one per request —

```
2026-10-26T07:56:28.326Z GET /hello
2026-10-26T07:56:28.334Z GET /invalid
```

Each line is three space-separated tokens with no literal brackets: the first matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$`, the second is exactly the request method, and the third is the query-stripped path, so `GET /hello?x=1` logs `... GET /hello`. The request answered with 404 is logged like any other. To count the lines without writing a file into the repository, start the server with its log outside the repository, drive the requests, stop the server by its pid, and count what it wrote:

```bash
# start the server, keeping its log outside the repository, and remember its pid
LOG=$(mktemp "${TMPDIR:-/tmp}/http-server.XXXXXX")   # a private file of your own, so the log never lands in the repository
node server.js > "$LOG" 2>&1 & pid=$!

# wait for the bind, issue the requests you want to count, then stop the server by its pid
sleep 1
curl -s -o /dev/null http://localhost:3000/hello
curl -s -o /dev/null http://localhost:3000/invalid
kill -INT $pid; wait $pid; echo "exit=$?"

# grep prints the number of request lines; wc -l counts every line, so it also includes the
# startup line and the two shutdown lines
grep -c 'Z ' "$LOG"
wc -l < "$LOG"

# remove the log file; nothing is left behind
rm -f "$LOG"
```

Run it as written and it prints `exit=0`, then `2` and `5`: the two requests it issues, and every line the server wrote — those two request lines plus the startup line and the two shutdown lines. The counts appear because only the server process receives `SIGINT`. Counting through a pipeline cannot produce them: a terminal sends `SIGINT` to every process in its foreground process group, so a pipeline such as `node server.js | grep -c 'Z '` delivers the signal to the counting filter as well, and the filter dies on its default disposition before it can print its total — the server is the only process that installs a handler. Signal the server process by its pid, as above; the block removes its log file at its end. The path comes from `mktemp`, given an explicit template in `${TMPDIR:-/tmp}`, which creates the file itself with a random name and owner-only permissions (mode `0600`), whereas a fixed name in the shared temporary directory can be pre-placed as a file or a symlink for the server's output to be written into.

**V6 — clean shutdown**

```bash
node server.js & pid=$!; sleep 1; kill -INT $pid; wait $pid; echo "exit=$?"
```

Expected output:

```
Server listening on port 3000
Received SIGINT. Shutting down gracefully.
Server closed. Exiting.
exit=0
```

Both shutdown lines appear, the process ends without a forced kill, and the status is 0. Interactively, `CTRL+C` in the server's terminal produces the same two lines and the same status.

**V7 — `GET /hello` under 10 ms (measurement)**

Warm up with one request, then take five samples:

```bash
curl -s -o /dev/null http://localhost:3000/hello
for i in 1 2 3 4 5; do curl -s -o /dev/null -w '%{time_total}\n' http://localhost:3000/hello; done
```

Expected: five values in seconds, each below `0.010`. The samples observed while verifying this README were `0.000478`, `0.000522`, `0.000466`, `0.000523` and `0.000518`; the figures vary with the host. The time is measured client-side by `curl --write-out` over loopback on the same host, so it includes curl's own process start and host scheduling; the server's own share is one string comparison and one `end()` write. A sample above 0.010 on an idle host points at host contention rather than server behaviour, which is why the check takes five samples rather than one.

**V8 — concurrency without blocking (measurement)**

```bash
for i in $(seq 1 50); do curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/hello & done; wait
```

Expected: 50 lines, every one `200`, and 50 corresponding request lines on the server's stdout for the same window — the burst is accepted together rather than serialised. On the host where this README was verified, all 50 were answered `200` within a single 7 ms window. The shell's own `wait` collects the background jobs, so no external tool is involved. File-descriptor and process limits are the only host-dependent factors, and 50 is well inside any default.

**V9 — memory stays stable (measurement)**

```bash
node server.js & pid=$!
sleep 1
ps -o rss= -p $pid
for i in $(seq 1 5000); do curl -s -o /dev/null http://localhost:3000/hello; done
ps -o rss= -p $pid
for i in $(seq 1 5000); do curl -s -o /dev/null http://localhost:3000/hello; done
ps -o rss= -p $pid
kill -INT $pid
```

Expected: three resident-set readings in kilobytes for the server process, none of them growing in proportion to the number of requests driven, and the third within 10 MB of the second. The 10 MB tolerance is the figure chosen for this check. On the host where this README was verified the three readings were `47384`, `59604` and `62180` KB — a rise of 2,576 KB between the second and the third — and all 10,000 requests were logged, one line each. Nothing is retained per request, so the heap reaches steady state during the first batch; because the JavaScript heap is collected on its own schedule, the check is stability across batches rather than an absolute number. Each batch is a shell loop of 5,000 requests and takes a couple of minutes.

**V10 — `PORT` overrides the default**

```bash
PORT=4000 node server.js
```

Expected stdout:

```
Server listening on port 4000
```

which names 4000 and not 3000, and `curl -si http://localhost:4000/hello` then returns the V2 contract on port 4000.

**V11 — invalid `PORT` refused**

```bash
PORT=abc node server.js; echo "exit=$?"
```

Run the same command with `3000.5`, `0x1F90`, `1e3`, `+3000`, `-1`, `65536` and `0` in place of `abc`. Expected stderr:

```
Invalid PORT value "abc": must be an integer between 1024 and 65535.
```

with the offending value quoted, no startup line on stdout at all, and

```
exit=1
```

The server never binds.

**V12 — privileged port refused**

```bash
PORT=80 node server.js; echo "exit=$?"
```

Run the same command with `1` and `1023`. Expected stderr:

```
Invalid PORT value "80": privileged ports 1-1023 are not permitted; choose a port between 1024 and 65535.
```

no startup line, and `exit=1`. No bind is attempted, so the check passes as an unprivileged user and produces no permission error.

**V13 — port already in use**

Hold port 3000 with one server, then start a second:

```bash
node server.js & pid=$!
sleep 1
node server.js; echo "exit=$?"
kill -INT $pid
```

Expected stderr from the second process:

```
Port 3000 is already in use. Stop the process using it or set PORT to a free port.
```

then `exit=1`, with no stack trace and no unhandled `error` event. While 3000 is still held, the companion check starts normally on another port:

```bash
PORT=3001 node server.js
```

```
Server listening on port 3001
```

**V14 — manifest fields and the start script**

```bash
npm pkg get name version description main scripts engines keywords
node -e "const p=require('./package.json'); console.log(p.name, p.version, p.main, p.scripts.start, p.engines.node, p.keywords.join(',')); console.log('deps?', 'dependencies' in p, 'devDependencies' in p, 'type' in p)"
CI=true npm start
```

The first command prints the seven fields as JSON, the second prints

```
nodejs-tutorial-hello-world 1.0.0 server.js node server.js >=18.0.0 nodejs,tutorial,http,server,beginner
deps? false false false
```

and `npm start` then prints the V1 startup line. The seven fields and their values are:

| Field | Value |
|-------|-------|
| `name` | `nodejs-tutorial-hello-world` |
| `version` | `1.0.0` |
| `description` | `Minimal Node.js HTTP server tutorial demonstrating single endpoint implementation` |
| `main` | `server.js` |
| `scripts.start` | `node server.js` |
| `engines.node` | `>=18.0.0` |
| `keywords` | `["nodejs", "tutorial", "http", "server", "beginner"]` |

There is no `dependencies` key, no `devDependencies` key and no `type` field.

**V15 — this README on a clean machine**

Follow this document from the top on a machine that has Node.js 18.0.0 or later and `curl`, and nothing else, installed: check the prerequisites, run the documented start command, run the documented `curl` command, then stop the server as documented. Expected: every documented command produces the output shown here, and the eight documentation topics — purpose, prerequisites, installation, execution, testing, expected output, shutdown and `PORT` configuration — are all covered by the sections above. No command in this README needs a file, package or directory that the repository does not contain.

**V16 — completeness and the footprint boundary**

```bash
ls -A
find . -maxdepth 1 -type f -printf '%f\n' | sort
node --check server.js
grep -n "TODO\|FIXME\|XXX\|TBD\|placeholder" server.js
grep -c "require(" server.js
```

Expected: the first command lists the three delivered files beside the repository's `.git` directory, and a working tree that also carries the pre-existing `blitzy/` research directory — as the one this repository was built in does — lists five entries:

```
.git
README.md
blitzy
package.json
server.js
```

`blitzy/` is a pre-existing research directory: it holds no deliverable and stands outside the three-file delivered set, so its presence is not a footprint failure. The second command lists exactly the delivered set —

```
README.md
package.json
server.js
```

with no `node_modules`, no `package-lock.json`, no test file and no CI file. `node --check server.js` prints nothing, which means the source parses cleanly. The marker grep finds no match: that check is scoped to `server.js`, the only code file, and this README contains those marker words only inside this command, as its search pattern. The last command prints

```
1
```

— exactly one `require(` call, and it names `node:http`.

## Troubleshooting

**`Invalid PORT value "abc": must be an integer between 1024 and 65535.`**

The value is not a decimal integer, or it is outside the accepted range — this includes `0`, `65536` and negative values. Fix: choose a port between 1024 and 65535, for example

```bash
PORT=4000 node server.js
```

**`Invalid PORT value "80": privileged ports 1-1023 are not permitted; choose a port between 1024 and 65535.`**

The value is a decimal integer below 1024. Fix: choose a port at or above 1024. There is no override variable for privileged ports, and you do not need root: the refusal happens before any bind is attempted, so running as an unprivileged user changes nothing.

**`Port 3000 is already in use. Stop the process using it or set PORT to a free port.`**

Something else is bound to that port. Fix: stop the process holding it, or start this server on another port:

```bash
PORT=4000 node server.js
```

**`Failed to start server: <error message>`**

The port could not be bound and the cause is not another process holding it. `<error message>` is the runtime's own description of the failure — the same substitution notation as in Configuration above, not text the program prints — so the message itself names what to act on. The exit status is `1` and nothing is left listening. Rule the configuration out with the override form and act on what the message says:

```bash
PORT=4000 node server.js
```

**Nothing is printed on startup**

The startup line is written from the listen callback, so it appears only once the port is actually bound. Check the exit status of the start command in the same shell, immediately after it returns:

```bash
echo $?
```

A status of `1` means the message on stderr names the cause — an invalid `PORT`, a privileged port, a port already in use, or another listen failure — and that message is what to act on.

**A request returns 404**

The method and the query-stripped path must both match exactly. `/hello` matches; `/hello/`, `/Hello`, `/HELLO`, `/hello%2F`, `POST /hello` and `HEAD /hello` do not. The query string is removed before comparison, so `/hello?anything=1` does match. Every non-match returns the same `Not Found` response, which deliberately does not say which check failed.

## Scope and non-goals

Each item below is a deliberate boundary of this tutorial rather than unfinished work. Nothing here is a step you are expected to add.

- **No tests and no test framework.** The gates under Verification are the test suite: they are commands you run and compare, and no test file or test dependency exists in the repository.
- **No CI/CD and no containers.** There is no workflow file, no `Dockerfile` and no compose file; the program runs with `node server.js` on your own machine.
- **No deployment automation and no process manager.** One foreground process, started and stopped by you.
- **No persistence.** No database, no cache, no file writes: nothing survives a request or a process exit.
- **No authentication, authorisation, sessions, cookies, CORS, CSRF or TLS.** The endpoint is anonymous, read-only and plain HTTP.
- **No rate limiting, quotas or request-size limits.** No protected resource and no request body to limit.
- **No endpoints besides `GET /hello` and the catch-all 404.** No health check, no metrics, no favicon or static file route, no redirect and no API version prefix.
- **No request-body or query-string parsing, and no content negotiation.** The request stream is never read, and the query string is only removed before the path comparison.
- **No structured logging, log levels or log files.** One line per request on stdout is the whole observability surface.
- **No `SIGTERM` or `SIGHUP` handling.** `SIGINT` is the one signal handled, and the five-second failsafe bounds the shutdown.
- **No TypeScript, build step, bundler or transpiler.** `server.js` is the file that runs.
- **No linter or formatter configuration.** Style is kept consistent by hand, and `node --check server.js` is the only static check.
- **No environment variables other than `PORT`**, and no `HOST` variable: the server binds the port alone.
- **No change to the `>=18.0.0` engine floor.** The code is written to run at that floor.
