# The Config Store Socket Protocol (`cs.sock`)

This document specifies the wire protocol spoken over `/var/tmp/cs.sock`, the
Unix domain socket the `$CONFIG_STORE` Compose volume mounts into a container.
It exists so a client can be written in a language other than Python without
reading `cp.py`'s source to reverse-engineer the protocol.

If your container's Python code just needs to talk to the Config Store, use
`cp.py` (see [ncos-sdk-reference.md](ncos-sdk-reference.md)) — do not
reimplement this protocol in Python. This document is for:

- A process in **another language**, in the **same container**, that needs
  direct Config Store access. Since `cs.sock` is already mounted into that
  container's filesystem namespace, it needs no adapter or proxy — just a
  client speaking this protocol. (An adapter is only justified when the
  consumer is in a *different* container, or is external to the router
  entirely — see "Giving More Than One Consumer Access to the Config Store"
  in [container-development-guide.md](container-development-guide.md).)
- Writing your own test mock of `cs.sock` for local development.
- An AI agent asked to generate a Config Store client in a language this repo
  doesn't already provide one for.

Everything here was observed against a live router (R980, NCOS 7.26.21) and
cross-checked against `cp.py`'s implementation — the canonical copy is at the
repository root. Where something is unconfirmed, it's marked as such rather
than stated as fact.

## Connection

- **Address:** `/var/tmp/cs.sock`
- **Family:** `AF_UNIX`, **type:** `SOCK_STREAM`
- **Availability:** only present when the service's Compose file lists the
  `$CONFIG_STORE` volume. Without it, the path does not exist. Check for the
  socket file before connecting so you can report a useful error rather than
  a generic connection failure.
- **Lifecycle:** open a new connection per request. `cp.py` never keeps a
  connection open between calls, and there's no evidence the Config Store
  supports or expects a persistent connection carrying multiple commands.
  Connect, send one command, read one response, close.
- **Set a receive timeout — non-negotiable.** A command sent with a missing
  or extra field does not error, it hangs the socket waiting for the field
  that never arrives. `cp.py` uses 2 seconds. Any client without a read
  timeout can block forever on a malformed command.

## Request format

A request is a sequence of newline-terminated ASCII fields, all in a single
`send`/`write` call:

```
<verb>\n<field1>\n<field2>\n...\n
```

- **Bare `\n`** terminates each field, including the last one. There is no
  CRLF and no other framing on the request side.
- **ASCII only.** Encode the whole command as ASCII before sending. Whether
  the Config Store accepts UTF-8 bytes is untested — don't risk it with data
  that might contain non-ASCII characters (see the `alert` sanitisation note
  below for the pattern to copy).
- **Field count is exact per verb and strict.** This is confirmed directly
  for `alert` (see below) — a live probe showed the two-field form hanging
  the socket rather than erroring. It is *not* independently confirmed for
  `get`/`put`/`post`/`delete`/`decrypt`; the same behavior is inferred for
  them from sharing the same protocol and dispatch mechanism, not tested one
  by one. Treat it as the safe assumption for every verb regardless — never
  build a command with a variable number of fields, always send every field
  a verb requires even if it's empty — but don't repeat the inference as a
  confirmed fact for verbs it hasn't actually been tested against.

### Verbs and their fields

| Verb | Fields (in order) | Notes |
|------|--------------------|-------|
| `get` | `path`, `query`, `tree` | `query` and `tree` may be empty strings / `"0"` |
| `decrypt` | `path`, `query`, `tree` | Same shape as `get`; decrypts encrypted values (e.g. certificate private keys) |
| `put` | `path`, `query`, `tree`, `value` | `value` is the JSON-encoded value, e.g. `true`, `"text"`, `42` |
| `post` | `path`, `query`, `value` | No `tree` field — three fields, not four |
| `delete` | `path`, `query` | Two fields |
| `alert` | `name`, `value` | Two fields, but see the strict-count warning below — this is the verb that most easily hangs a naive client |

Examples, showing the literal bytes sent (with `\n` written out for clarity):

```
get\nstatus/gps/fix\n\n0\n
put\nconfig/system/gps/enabled\n\n0\ntrue\n
post\nconfig/system/sdk/appdata\n\n{"name": "poll_interval", "value": "1.0"}\n
delete\nconfig/wan/rules2/abc123\n\n
alert\nmy_app\ntank level critical: 92%\n
```

`value` for `put`/`post` is always JSON-encoded, even for a bare string —
`json.dumps("text")` produces `"text"` with the quotes included, not `text`.

### The `alert` verb specifically

Confirmed on hardware: `alert` sends a custom alert to NCM using only the
Config Store socket — no on-router SDK application registration is needed.

```
alert\n<name>\n<value>\n     ->  status: ok, body: Alert added('<name>: <value>')
alert\n\n<value>\n           ->  status: ok, body: Alert added('<value>')
alert\n<value>\n             ->  no reply at all; the socket blocks waiting
                                  for the missing third field
```

- Exactly three newline-terminated fields (`alert`, `name`, `value`) — the
  same "hangs rather than errors" rule as above, but called out again here
  because this is the verb most likely to be hand-built with a variable
  field count (e.g. omitting `name` when not needed). Always send the verb
  and both fields, even if `name` is an empty string.
- `name` is a **prefix** on the delivered text, not a separate field NCM
  displays — the console shows only `value`. Don't rely on `name` to
  distinguish sources.
- Newlines and tabs in `value` (and `name`) must be stripped or replaced with
  spaces before sending — the protocol is newline-delimited, so an embedded
  newline injects an extra field and desyncs the command.
- An empty `value` still creates an NCM alert, showing the placeholder
  "Router NCOS App Generated Alert" with no detail. Refuse to send empty
  alert text at the client level; the router will not refuse it for you.
- Response body for `alert` is a **plain string**, not JSON — see "Response
  body encoding" below.

## Response format

The response is an HTTP-like header block followed by a body:

```
status: <word>\ncontent-length: <bytes>\n\r\n\r\n<body>
```

Two details that will break a naive parser, both confirmed against a live
router:

- **Header fields are separated by a bare `\n`, but the header block is
  terminated by `\r\n\r\n` (CRLFCRLF).** For example:
  `status: ok\ncontent-length: 90\n\r\n\r\n<body>`. Splitting the whole
  response on `\r\n` does **not** yield the individual header fields — search
  for the `\r\n\r\n` terminator first, then parse the bytes before it as
  `\n`-separated `key: value` lines.
- **`content-length` counts the body only**, and is accurate. Read exactly
  that many bytes after the terminator; don't rely on the socket closing to
  signal end-of-body, and don't assume one `recv()` call returns the whole
  response — loop until you have `content-length` bytes.

### Parsing algorithm

1. Read from the socket into a buffer until the buffer contains `\r\n\r\n`
   (or your receive timeout expires — treat a timeout here as "malformed or
   hung response", not as success).
2. Split the bytes before `\r\n\r\n` on `\n` to get header lines. Parse
   `status: <value>` and `content-length: <value>` from those lines. Do not
   assume they appear in a fixed order.
3. Bytes already read past the `\r\n\r\n` terminator are the start of the
   body. Keep reading until you have `content-length` bytes total for the
   body.
4. Decode the body — see below.

### Response body encoding

- **Most verbs return a JSON body.** `get`/`decrypt` on a path holding an
  object or array return the corresponding JSON structure; a scalar path
  returns the JSON-encoded scalar (`true`, `42`, `"connected"`).
- **Some verbs return a plain string, not JSON.** `alert`'s success body
  (`Alert added('...')`) is plain text, and some `put` error bodies are also
  plain messages rather than JSON. **Try `json.loads()` (or your language's
  equivalent) and fall back to the raw decoded text if it fails** — don't
  assume JSON and let a parse error crash the client.
- A response holding no meaningful data (a `get` on an absent path, for
  example) is `data: null` in JSON terms, i.e. the body is the literal bytes
  `null`. This is indistinguishable from a path that exists and is genuinely
  empty — the protocol has no separate "not found" signal. See "Ambiguity of
  empty results" below.

### `status` values observed

| Status | Meaning |
|--------|---------|
| `ok` | Request succeeded |
| *(firmware-dependent string)* | The exact success/failure vocabulary beyond `ok` is not fully catalogued and may vary by firmware version. **Never branch application logic on the exact `status` string for a write's success** — confirm a `put`/`post`/`delete` actually took effect by reading the path back afterward. This is a "the response format" note; the earlier `alert` fields note it too, since `alert` is the specific case that's been verified end-to-end. |

Client-side synthetic statuses (produced by your own parsing code, not sent
by the router) are useful to distinguish transport failures from protocol
responses — `cp.py` uses `timeout` and `malformed` for exactly this, and
returns an empty result rather than raising so a long-running poller survives
a single bad response.

## Ambiguity of empty results

Reading a path that doesn't exist and reading a path that exists but holds no
data both produce the same response: a body of `null` (or an empty
structure). The protocol never distinguishes "absent" from "empty" at this
layer. Two practical consequences:

- **Don't guess paths.** A guessed path that's wrong returns `null` just like
  a correct path with no data, so trial and error produces confident wrong
  conclusions rather than errors. Resolve exact paths from
  [ncos-api/config/PATHS.md](ncos-api/config/PATHS.md) or the DTD
  (`ncos-api/config/dtd/`) first.
- **Probe a path known to always have data** (`status/product_info` is what
  `cp.py` uses) to distinguish "the whole socket is unreachable" from "this
  specific path has no data." A missing `$CONFIG_STORE` volume and a router
  with genuinely no data at a path look identical otherwise.

## Failure modes to handle explicitly

| Symptom | Cause | Client behavior |
|---------|-------|------------------|
| Socket path doesn't exist | `$CONFIG_STORE` volume not attached | Check `os.path.exists`-equivalent before connecting; report this distinctly from a connection refusal |
| Connection refused / times out | Config Store service down, or genuinely transient | Retry with backoff for a long-running process; don't crash a poller on one failure |
| Read times out mid-response | Malformed command (see field-count warnings above), or an unresponsive Config Store | Always set a receive timeout; treat a timeout as a failed request, not a hang |
| `\r\n\r\n` never found, or `status`/`content-length` missing | Truncated or unrecognized response | Report as malformed rather than crashing on a failed parse — don't let a regex/parse failure raise past the caller and destroy the real error context |
| Body fails to parse as JSON | Expected for some verbs (see above) | Fall back to raw text; don't treat this as an error condition by itself |

A repeatedly unreachable socket is worth throttling in your own logging if
the client polls: logging every failed attempt means a permanently missing
volume produces one log line per poll forever. Log the first failure, then
every Nth (`cp.py` logs the first, then every 60th).

## Minimal reference implementation (pseudocode)

```
function dispatch(command_string):
    socket = connect_unix("/var/tmp/cs.sock")
    socket.set_timeout(2.0)
    socket.send(command_string.encode("ascii"))

    buffer = bytes()
    header_end = -1
    while header_end < 0:
        chunk = socket.recv(8192)
        if chunk is empty: break
        buffer += chunk
        header_end = buffer.find(b"\r\n\r\n")

    header_text = buffer[:header_end].decode()
    status = extract_field(header_text, "status")
    content_length = int(extract_field(header_text, "content-length"))

    body = buffer[header_end + 4:]
    while len(body) < content_length:
        chunk = socket.recv(8192)
        if chunk is empty: break
        body += chunk

    socket.close()
    body_text = body.decode(errors="replace")
    try:
        data = json_parse(body_text)
    except:
        data = body_text.strip()

    return {status: status, data: data}


function get(path, query="", tree=0):
    return dispatch(f"get\n{path}\n{query}\n{tree}\n").data

function put(path, value, query="", tree=0):
    return dispatch(f"put\n{path}\n{query}\n{tree}\n{json_encode(value)}\n")

function post(path, value, query=""):
    return dispatch(f"post\n{path}\n{query}\n{json_encode(value)}\n")

function delete(path, query=""):
    return dispatch(f"delete\n{path}\n{query}\n")

function alert(name, value):
    return dispatch(f"alert\n{name}\n{value}\n")
```

This maps directly onto sockets in most languages:

- **Go:** `net.Dial("unix", "/var/tmp/cs.sock")`, then plain `net.Conn`
  reads/writes; use `conn.SetDeadline()` for the timeout.
- **Node.js:** `net.createConnection({ path: '/var/tmp/cs.sock' })`; use
  `socket.setTimeout()`.
- **Rust:** `std::os::unix::net::UnixStream::connect(...)`, with
  `set_read_timeout`.
- **C:** `socket(AF_UNIX, SOCK_STREAM, 0)` + `connect()` to a
  `sockaddr_un`, with `SO_RCVTIMEO` for the timeout.
- **Java:** `AFUNIXSocket` (junixsocket library) or, on Java 16+,
  `java.net.UnixDomainSocketAddress` with `SocketChannel`.

## Appdata: a structural convention, not a separate protocol

User-configurable settings surfaced in NCM (System > SDK Data) live at the
fixed path `config/system/sdk/appdata` as a JSON list of
`{"_id_": ..., "name": ..., "value": ...}` objects, all string-valued. There
is no dedicated verb for this — it's just `get`/`put`/`post`/`delete` against
that path and its indexed children (`config/system/sdk/appdata/<_id_>/value`
for updating one entry in place). See `cp.py`'s `get_appdata`/`put_appdata`
for the exact create-vs-update logic (check for an existing entry by `name`
first; `put` the `value` field if found, otherwise `post` a new entry) and
replicate that logic rather than reinventing it, since getting the
create/update branch wrong produces duplicate entries.

**Verify any write by reading it back.** `put`/`post`/`delete` do not
reliably signal failure in their response `status` — a client that reports
"success" straight from the response, without confirming the value actually
changed, can report success for a write that silently didn't happen (a real
observation from testing against this protocol, not a hypothetical).

## What this protocol cannot do

- **No event subscriptions.** There is no verb here for subscribing to
  changes — that requires a separate event socket not exposed to containers
  (unverified whether it works from a container at all; assume it doesn't).
  Poll `get()` on an interval and diff against the previous value instead.
- **No transport-level authentication.** Anything that can open this Unix
  socket — i.e. anything running inside the same container namespace — has
  full read/write/alert access to router configuration. This protocol is not
  meant to be exposed beyond the container it's mounted into; see
  "Giving More Than One Consumer Access to the Config Store" in
  [container-development-guide.md](container-development-guide.md) for how
  to scope access when a consumer is outside that boundary.

## Testing a client without a router

Bind a mock `AF_UNIX` listener to a temporary path and reply in this wire
format — this is exactly how `cp.py` itself is tested. Cover at minimum:

- A well-formed `get` response with a JSON body
- A well-formed response with a plain-string body (simulating `alert` or a
  `put` error)
- A truncated response (connection closes before `content-length` bytes
  arrive)
- A response the client never receives (to confirm your read timeout fires
  instead of hanging)
- The socket path simply not existing (to confirm your client reports this
  distinctly from other connection failures)

See "Verifying Before Deployment" in
[container-development-guide.md](container-development-guide.md) for the
broader pattern this fits into.
