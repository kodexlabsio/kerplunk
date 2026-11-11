# HTTP API

`konstruct serve` answers questions about the graph over HTTP, under `/api/v1/`, for tools that cannot run Python or speak MCP. The contract is an OpenAPI 3.1 document, served at `/api/v1/openapi.json` and published here as {download}`openapi.json`; both are made from the server's own route table, so they describe exactly what it serves.

```bash
konstruct serve --no-build &
curl -s localhost:8000/api/v1/impact/model.jaffle_shop.stg_payments%23amount | jq .summary
```

The API is read-only. It never writes a file, runs SQL or connects to a warehouse; `konstruct verify` and `konstruct build` are not part of it.

## The Viewer

`/` is the viewer, the same page as `graph.html` but for one element in its head, `<meta name="konstruct-serve" content="">`, which makes it load `/graph.json` and `/explain.json` from the server rather than from the page. `/explain.json` holds what `graph.html` embeds beside the graph: each column's formula and each relation's conditions. Both carry strong ETags and are sent with gzip or brotli when the client accepts them.

`/events` is a stream of server-sent events. An unnamed event arrives on connecting and again each time a build replaces `graph.json`; its `id` is the graph's version and its data is `{"version", "nodes", "edges"}`. The viewer reloads the graph in place on each one, keeping the page and the view. At most 64 streams are open at once (`too-many-streams` below).

## Endpoints

Every endpoint takes `GET` (and `HEAD`) and answers JSON unless noted. Each calls the same Python function as the command line and the MCP server, so the three give one answer. Endpoints with `{id}`, `from` and `to` name nodes by id (percent-encode `#` as `%23`); the others take any name the command line takes. An answer is kept until the graph changes, except where it reads git or the project's files. Python answers one question at a time: a kept answer is sent at once, and the others wait their turn, up to eight at once ([busy](#busy)).

| Endpoint | Answer | Kept |
|----------|--------|------|
| `/api/v1/nodes/{id}` | The node as `graph.json` holds it, its label, and its incoming and outgoing edges | yes |
| `/api/v1/nodes?kind=&limit=&cursor=` | Nodes by kind (default: relations), paged | yes |
| `/api/v1/resolve?name=` | The nodes a name such as `orders.amount` refers to | yes |
| `/api/v1/impact/{id}?depth=` | Everything downstream, as `konstruct impact --json` | yes |
| `/api/v1/trace/{id}?depth=` | Everything upstream, as `konstruct trace --json` | yes |
| `/api/v1/path?from=&to=` | The shortest connection between two nodes | yes |
| `/api/v1/search?q=&kind=&limit=&cursor=` | Nodes ranked by the words of `q`, paged | yes |
| `/api/v1/describe/{ref}` | One node in detail, as `konstruct describe` | yes |
| `/api/v1/explain/{ref}` | A column's formula, composed down to its sources | yes |
| `/api/v1/define?ref=` | A business term, or the terms bound to a column | yes |
| `/api/v1/terms?limit=&cursor=` | Every business term, paged | yes |
| `/api/v1/ontology` | Entities, measures, relationships and broken rules | yes |
| `/api/v1/conflicts?limit=&cursor=` | Columns that compute one business idea in different ways, paged | yes |
| `/api/v1/query?q=&depth=` | A question in words, as `konstruct query` | yes |
| `/api/v1/history/{ref}?limit=` | When a column's formula changed in git | no |
| `/api/v1/diff?base=&head=` | What a change does, column by column | no |
| `/api/v1/gate?base=` | The gate's findings, for the graph as served (`konstruct gate --no-update`) | no |
| `/api/v1/status` | What Konstruct finds in the project, and whether the graph is current | no |
| `/api/v1/report` | The figures REPORT.md summarises | yes |
| `/api/v1/export/{format}?select=&columns=&namespace=` | `openlineage` (JSON lines), `mermaid`, `dot`, `csv` or `ossie` (YAML), as text | yes |
| `/api/v1/openapi.json` | The OpenAPI document | yes |

Each answer is the `to_dict()` of the matching result in the [Python API](python-api.md), so its fields are documented there.

```bash
curl -s 'localhost:8000/api/v1/search?q=lifetime+value&kind=column&limit=5'
curl -s 'localhost:8000/api/v1/path?from=seed.jaffle_shop.raw_payments&to=exposure.jaffle_shop.customer_dashboard'
curl -s localhost:8000/api/v1/explain/customers.customer_lifetime_value | jq .composed
curl -s 'localhost:8000/api/v1/export/mermaid?select=orders.amount&columns=true'
```

## Paging

Lists (`nodes`, `terms`, `search` and `conflicts`) return one page and a `next_cursor`, which is `null` on the last page. Pass it back as `cursor` with the same parameters for the next page. `limit` is from 0 to 1000. The order is the same on every page, and a cursor belongs to one version of the graph: after a rebuild it is refused with `stale-cursor`, and paging starts again.

```bash
curl -s 'localhost:8000/api/v1/nodes?kind=column&limit=100' | jq .next_cursor
curl -s 'localhost:8000/api/v1/nodes?kind=column&limit=100&cursor=4f1c2a90b3e8d7a6.100'
```

## Errors

Errors are RFC 9457 problem details, sent as `application/problem+json`:

```json
{"type": "https://kodexlabsio.github.io/konstruct/reference/http-api.html#no-such-node", "title": "Nothing in the graph has this id or name", "status": 404, "detail": "nothing in the graph matches 'orders.amont'; did you mean orders.amount?"}
```

The `type` is one of these, and does not change within `/api/v1/`:

(bad-request)=
### bad-request

400. The request is malformed, such as one with two `Host` headers.

(invalid-parameter)=
### invalid-parameter

400. A required parameter is missing, or one is not of its type or not one of its values. A `base`, `head` or `ref` that starts with `-` is refused, since git would read it as an option.

(unauthorised)=
### unauthorised

401. The server needs its token (below).

(host-not-allowed)=
### host-not-allowed

403. The `Host` header does not name the server. Requests must name it by IP address, `localhost` or the `--host` given, which stops other sites reaching it through DNS rebinding.

(cross-site)=
### cross-site

403. A browser sent the request from a page on another site (`Sec-Fetch-Site` is `cross-site` or `same-site`, or `Origin` names another origin). Only the viewer itself, at `/`, may be opened from a link elsewhere; its files, events and the API are for the viewer and for clients that are not browsers.

(not-found)=
### not-found

404. No route has this path.

(no-such-node)=
### no-such-node

404. No node has this id, or nothing matches the name.

(method-not-allowed)=
### method-not-allowed

405. Only `GET` and `HEAD` are served.

(stale-cursor)=
### stale-cursor

409. The graph was rebuilt after the cursor was made.

(not-answerable)=
### not-answerable

422. The question does not apply: a name that is ambiguous, `explain` of a table, `diff` outside a git repository.

(internal-error)=
### internal-error

500. The answer failed; `detail` says why, with each file named but not where it is. The server's output has the whole error.

(busy)=
### busy

503, with `Retry-After: 1`. Eight questions are waiting for an answer already. The answers come from the Python API one at a time, so a question that is not kept (above) waits for those before it; a kept answer is sent at once, busy or not. Ask again after a moment.

(too-many-streams)=
### too-many-streams

503. 64 `/events` streams are open already, one for each open viewer page; the connection is closed. Close a page, or reload once another has closed.

## Access

By default the server listens on 127.0.0.1, and only this machine can connect. With `--host` set to another address it requires a bearer token on every request: `KONSTRUCT_SERVE_TOKEN` when it is set, otherwise one made at start-up and printed in the address. The viewer's address carries it as `?token=`, which sets a cookie for the page's own requests; other clients send it as a header:

```bash
KONSTRUCT_SERVE_TOKEN=$(openssl rand -hex 24) konstruct serve --host 0.0.0.0
curl -s -H "Authorization: Bearer $KONSTRUCT_SERVE_TOKEN" http://10.0.0.5:8000/api/v1/status
```

The server speaks plain HTTP, so the token and every answer cross the network unencrypted. Before using `--host` beyond a network you trust, put a proxy that terminates TLS in front of the server (Caddy, nginx or a cloud load balancer), keep `--host` on an address only that proxy can reach, and have the proxy pass the `Host` the server was started with.

Other machines reach the server by its IP address or by the name given to `--host`. Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a Content-Security-Policy, and none allows other origins to read it.
