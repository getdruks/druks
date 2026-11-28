---
title: "Configuration"
description: "Configure deployment settings, access control, traces, harnesses, services, sandboxes, notifications, MCP servers, and skills."
icon: "settings"
---

Druks has two authored configuration planes. Use `druks.toml` for process and
deployment topology. Use the dashboard for operator choices that can change
without replacing the process.

| Plane | Examples | Stored in |
| --- | --- | --- |
| Deployment | installation timezone, identity, ingress, Drukbox | `~/druks/druks.toml` |
| Dashboard | personal timezone, the GitHub connection, harness and tracker credentials, workflow and instruction overrides, MCP servers, skills | Postgres |

The installer creates the deployment `.env` from `druks.toml`. Compose, Druks,
and Drukbox consume this build artifact. Edit `druks.toml`, then run the
installer again to apply changes. `druks setup` creates `.env` but does not
restart services.

Do not edit `.env`, with one exception. Its last section holds the
[secrets](#secrets), and the installer keeps that section.

The file location determines its format. Repository files such as
`.druks/software_factory/config.yml` use YAML. Operator files on the host use
TOML because the installer must create the environment without value changes.
These files share no keys or readers.

`druks.toml` is the authority for authored process configuration. Druks reserves
environment variables for infrastructure that Compose injects, such as database,
Redis, data, and container paths.
[`.env.example`](https://github.com/getdruks/druks/blob/main/.env.example) is the
host-run development template for that environment plane.

## Deployment file

`druks.toml` has one table per operator concern:

| Table | Purpose |
| --- | --- |
| `[identity]` | Browser identity mode and header or JWT verification inputs |
| `[urls]` | Dashboard callback base URL, public webhook hostname, and the URL of `/mcp` that a box dials |
| `[apps.<name>]` | An app switched off in this installation |
| `[services.<slug>]` | Service credentials managed by deployment configuration, or a service switched off |
| `[paths]` | Host data and harness configuration paths |
| `[sandbox]` | Drukbox provider, service URL, image override, registry host and user name, and the proxy and issuer addresses |
| `[sandbox.<provider>]` | Provider environment passed through to the remote stack |
| `[browser]` | Browser egress proxy scope and timezone |
| `[traces]` | The OTLP endpoint that takes the [traces](#traces) |
| `[providers.anthropic]` | The Claude sign-in method the installation offers |
| `[env]` | Additional deployment environment settings rendered verbatim |

A blank string means unset, and the installer omits it from `.env`. Use `[env]`
for settings without another `druks.toml` home, including additional `DRUKS_*`
settings. The installer reports a key that it already owns as a configuration
gap, and it keeps the canonical value.

On a remote shape, `[sandbox.<provider>]` accepts the variables that
[Drukbox](https://github.com/czpython/drukbox) documents. Druks does not enumerate
providers. `docker` and `exe` select shape-specific first-write templates. Every
other provider name selects the generic remote shape. Drukbox validates it.

The local `docker` shape does not render `[sandbox.<provider>]`. Its Drukbox
service gets its environment from the defaults in `deploy/compose.yaml`.

Use repeatable `druks setup ... --set key.path=value` arguments for explicit
scripted writes.

## Secrets

`druks.toml` holds no secret. The secrets of Druks are `secrets_key`,
`database_url`, `redis_url`, `sandbox.service_token`, `browser.proxy`, and
`traces.headers`. Druks takes each one from an environment variable. The name is
`DRUKS_` and the key path, with `_` for each dot: `DRUKS_SECRETS_KEY`,
`DRUKS_SANDBOX_SERVICE_TOKEN`. Druks does not start when `druks.toml` holds a
secret.

### Secrets of an installation

The installer keeps the secrets in the last section of `~/druks/.env`:

| Variable | Reader | From |
| --- | --- | --- |
| `DRUKS_SECRETS_KEY` | Druks | The installer. The [vault key](#credential-custody-and-secrets-at-rest) |
| `DRUKS_SANDBOX_SERVICE_TOKEN`, `SERVICE_TOKENS` | Druks, Drukbox | The installer. One token, under the name of each reader |
| `DRUKS_POSTGRES_PASSWORD` | Postgres, and the database URLs that Compose builds | The installer |
| `SECRETS_KEY` | Drukbox | The installer. The Drukbox key |
| `REGISTRY_PASSWORD` | Drukbox | You, for private sandbox images |
| `DRUKS_BROWSER_PROXY` | Druks | You, for a browser egress proxy |
| `DRUKS_TRACES_HEADERS` | Druks | You, for the headers that your [trace collector](#traces) requires |
| `DRUKS_DATABASE_URL`, `DRUKS_REDIS_URL` | Druks | You, only for a database or a Redis that Compose does not run |
| A provider secret, for example `EXE_API_TOKEN` | Drukbox | You |

The installer makes its secrets one time and does not change a value that is
there. It keeps each line of the section when it renders `.env` again. To set a
secret that only you know, add its line to the section. The local shape needs no
secret from you.

Druks does not enumerate providers. Put a plain provider variable in
`[sandbox.<provider>]` and a provider secret in the secrets section. The
installer reports a variable that is in both places. It moves a known secret
that it finds in `druks.toml` to the section, so `druks setup ... --set` also
sets one.

Apply a changed secret with `docker compose up -d`. The exception is
`DRUKS_POSTGRES_PASSWORD`: Postgres uses it only when it creates the database.

`.env` is the only copy of these secrets. When you move or recover an
installation, preserve it.

### Secret files

Druks can also read a secret from a file, for a platform that delivers secrets
as files. Set `DRUKS_SECRETS_DIR` to the directory, for example a mounted
`/run/secrets`. The file name is the key path: `secrets_key`, `redis_url`,
`sandbox.service_token`. Druks does not start when a secret is in the
environment and in a file. The installer does not use files.

A file can also set a `Secret` field of an app's settings. Name the file
`apps.<app>.<field>`, for example `apps.field_notes.sync_token`. When the file
exists, its content is the value.

The app settings form shows the field as **Managed by configuration**, and nobody
can change or clear it there. When the file is absent, the field keeps the value
that an admin saved, or stays unset. Plain fields of app settings stay on the
dashboard. Druks reads the files at startup, so restart Druks after a change.

Druks does not start when a file `apps.<app>.<field>` names one of these:

- An app that is not installed or not enabled.
- A field that the app does not declare.
- A field that is not a `Secret`.
- `enabled`: only `[apps.<name>] enabled` in `druks.toml` switches an app on or
  off.

The error names the file. When the app's settings refuse a value, the read of the
settings fails with an error that does not show the value.

## Managed services

A `[services.<slug>]` table makes that service managed. `druks.toml` and the
secret files own its card, and the Services page cannot change or disconnect it.
The table is for a platform that writes `druks.toml` itself. The installer
renders nothing from it and writes it back as it is. The table holds the plain
fields of the card. Each secret field of the card is a file in
`DRUKS_SECRETS_DIR`:

```toml
[services.waha]
url = "http://control.example/_instance/<uuid>/waha"
```

Here the key is the file `services.waha.api_key`. Druks logs and skips a table
for a service that no installed app declares. Druks does not start when a table
holds an invalid field. Each managed card shows **Managed by configuration**.

Druks reads the tables at startup, so restart Druks after a change. When you
remove a table, the card keeps its credentials, and you can change it on the
Services page again.

## Disabled apps and services

One Druks image serves installations with different purposes. A platform that
writes `druks.toml` itself switches an app or a service off for one
installation. The installer renders nothing from these tables. It writes them
back as they are, as it does with `[services.<slug>]`. `druks setup` then renders
`.env` from a file that holds them.

```toml
[apps.software_factory]
enabled = false

[services.jira]
enabled = false
```

`enabled` defaults to true. A disabled app or service does not exist in this
installation. Druks does not load a disabled app, so its pages, its settings,
and the services that it declares are absent. A disabled service is absent from
the Services page and from `druks doctor`.

An app that uses a disabled service fails as it fails when nobody connected the
service. Nothing changes stored data. The card and the sign-ins of a disabled
service stay in the vault. The runs of a disabled app stay as they are.

Druks does not start when:

- `[apps.<name>]` names an app that no installed package provides.
- `[apps.<name>]` holds a key other than `enabled`.
- `enabled` is not a boolean, in either table.
- `[services.<slug>]` sets `enabled = false` and holds a card field.

Generate migrations with every app enabled. With an app disabled,
`makemigrations` proposes to drop its tables.

## Personal and installation settings

One Druks installation serves one organization. Separate organizations use
separate installations. **Settings → Instructions** owns the shared harness,
model, billing, effort, fast mode, and timeout defaults. Every account uses
these defaults. Shared instruction overrides take priority. A declared
instruction timeout also takes priority over the installation default.

Effort is one of `low`, `medium`, `high`, `xhigh`, and `max`. Claude and Pi
take the level as it is. Codex's scale ends at `xhigh`, so it runs `max` as
`xhigh`. OpenCode ignores the level: see [Chat](chat.md).

**Include apps** is off by default. When it is on, a run loads the apps of the
subscription that it runs on in its sandbox: claude.ai connectors or ChatGPT apps.
For an agent, see [Talk to an agent](chat.md#talk-to-an-agent).
See [Harnesses](#harnesses) for what the switch changes in a sandbox.

**Settings → Preferences** edits, on your `Account`, the timezone, the theme, and where
your notifications go. Druks copies the installation timezone when it
creates an account. Later installation changes do not change existing account
preferences. Each page saves its own draft. See [Notifications](#notifications).

Set the installation timezone at the top level of `druks.toml`, before the first table:

```toml
timezone = "Europe/Madrid"
```

Use an IANA timezone. The default is `UTC`. Restart Druks after a change to
apply it. A schedule that a person asked for keeps that person's timezone.

The first account becomes the default account, including in header and JWT modes.
Unattended calls use its subscriptions. The default account has no extra
permissions.

Calls with an explicit account use that account's subscriptions. Missing
subscriptions fail the call. API keys belong to the installation.

The unattended account must be able to use each app that runs on a schedule.
If it is a member, an admin adds it to a team that lists each scheduled app.

The first account is also an admin. Each later person joins as a member when
Druks first resolves their identity. An admin changes a person's role in
**Settings → People**. Only an admin can change shared settings, app settings,
installation connections, provider keys, browser profiles, MCP servers, and
notification destinations. A member can change personal preferences,
subscriptions, API tokens, and per-person connections and keys. A member can
also pair a phone with a private Bot's number in **Settings → Phones**.

The installation timezone controls the schedules that workflows declare,
operational day boundaries, and local time in the chats of a number's bot and bot
admin accounts. Your personal timezone controls timestamp display and
local time in your chats: the time that an agent reads, your reminders, and the
schedules that you ask an agent for. Gate notifications use the
run account's preferences. Unattended runs record the default account.
Druks refuses to start a run before account setup.

**Schedules**, below **Usage**, lists workflows that declare a schedule. You can
change cadence, pause, or resume each schedule. Preset and pause changes save
automatically. Custom cron input saves when you press Enter or leave the field.
**Use defaults** removes both overrides and restores the declared cadence and
enabled state. App settings show the same fields.

The page shows the installation timezone. Account timezone preferences do not
change the timing of these schedules. A failed save keeps your edits. A read refresh also
preserves unsaved edits. If you leave the page with unsaved edits, Druks asks
first. The `?app=` filter selects one installed app.

**Next run** estimates the next invocation from the saved cadence. It does not
confirm scheduler health. **Last run** and **Last runs** show recorded schedule
invocations from DBOS, including dispatch ticks. They do not show the downstream
work that dispatch starts. History contains up to eight invocations per schedule.

**Run now** queues one invocation through DBOS without a change to the cadence or
pause state. A paused schedule stays paused. The invocation uses the same entry
and default account as its scheduled ticks.

The API exposes installation settings at `GET/PATCH /api/settings` and your
preferences at `GET/PATCH /api/settings/personal`. The personal route uses the
authenticated account. It returns `timezone`, `theme`, and
`notificationChannelId`. It rejects execution settings. The installation API rejects
timezone changes.

## Core process settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUKS_DATABASE_URL` | local `druks` Postgres | Runtime and DBOS database. A [secret](#secrets) |
| `DRUKS_DATABASE_POOL_SIZE` | `20` | Connections each process keeps open for requests and workflow steps |
| `DRUKS_DATABASE_MAX_OVERFLOW` | `30` | Extra connections each process opens under load and closes after use |
| `DRUKS_DBOS_POOL_SIZE` | `20` | Connections each process keeps for DBOS |
| `DRUKS_TEST_DATABASE_URL` | local `druks_test` Postgres | What the shipped pytest fixtures use — never the runtime's |
| `DRUKS_TEST_REDIS_URL` | `redis://127.0.0.1:6379/15` | What the shipped pytest fixtures flush |
| `DRUKS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Short-lived coordination and caches. A [secret](#secrets) |
| `DRUKS_DATA_DIR` | `/var/lib/druks` | Logs, artifacts, Druks files, installed skills |
| `DRUKS_HARNESS_CONFIG_ROOT` | `~/.config/druks/harnesses` | Optional harness configuration copied into sandboxes |
| `DRUKS_LOG_LEVEL` | `INFO` | Python and DBOS log level |

`log_format` sets the format of the log. It is a top-level key of `druks.toml`,
before the first table, and it has no environment variable:

```toml
log_format = "json"
```

With the default `text`, each record is one line of text:
`<time> <level> <logger> <message>`, and a traceback continues on the next
lines. DBOS, FastMCP, and uvicorn write in their own text formats.

With `json`, each record is one JSON object on one line, with `time` in UTC,
`level`, `logger`, `message`, and `exception`. `exception` holds the traceback,
or `null` when the record has none. Inside a span that Druks sends, a record also
holds `trace_id` and `span_id` in hex. The records of DBOS, FastMCP,
and uvicorn, and the migration lines of `druks init-db`, take the same form, on
standard error. Restart Druks after a change.

Each Druks process can open up to `DRUKS_DATABASE_POOL_SIZE` +
`DRUKS_DATABASE_MAX_OVERFLOW` + `DRUKS_DBOS_POOL_SIZE` + 1 Postgres connections.
The extra connection is the DBOS notification listener. With the defaults, this
is 71 of the 100 connections that Postgres allows by default.

Postgres stores durable state. Redis does not store workflow state. It supports
short-lived concerns including webhook delivery claims, OAuth state and token
caches, and the sandbox provisioning gate.

## Traces

Druks sends traces over OTLP when you name an endpoint. Without an endpoint,
Druks records no span and sends nothing.

```toml
[traces]
endpoint = "https://collector.example.com/v1/traces"
```

`traces.endpoint` is the full URL of the traces endpoint of your collector,
with its path. Every collector that takes OTLP works. Only this key enables traces:
the `OTEL_EXPORTER_OTLP_ENDPOINT` variable of OpenTelemetry does not. The
headers that the collector requires, such as an API key, are the secret
`traces.headers`. Set `DRUKS_TRACES_HEADERS` in the
[secrets](#secrets-of-an-installation) section of `.env`, as `name=value` pairs
separated by commas:

```text
DRUKS_TRACES_HEADERS=x-api-key=<key>,x-team=<team>
```

Druks sends protobuf over HTTP, never JSON. It ignores
`OTEL_EXPORTER_OTLP_PROTOCOL` and `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`. It sends
the spans in batches, and it sends the last batch when the process stops.

A trace shows:

- A span for each execution of a run, named after the workflow. It carries
  `druks.app` and `druks.run_id`. A run about a subject also carries
  `druks.subject_type` and `druks.subject_id`.
- A span for each durable step of the run, under the span of the run. DBOS
  makes these spans.
- A `gate_wait <gate>` span for each wait at a gate, from the park to the
  answer. It carries `druks.gate` and `druks.run_id`. A wait that a timeout or a
  cancel ends shows as an error. A person spends the time at a gate. The working
  time of a run is its span minus its gate waits.
- An `agent_call <agent>` span for each agent call, also for a call inside a
  `@step`. It carries `druks.agent`, `druks.run_id`, `druks.harness`, and
  `druks.model`. When the harness reports usage, the span also carries the
  token counts as `druks.usage.*` and the cost in US dollars as
  `druks.cost_usd`.
- A span for each request to Druks, each database statement, and each call
  that Druks makes through httpx. The work in a sandbox has no spans: the span
  of the step that waits for it covers it.

Each span names its service, `druks`, as `service.name`. It also names the host
of the webhook URL as `druks.host`, so a collector can tell one installation
from another. Druks keeps the trace of every run. A
[recovered run](concepts.md#durability-and-recovery) shows in a second trace.

Three kinds of repeated work send no span, so that they do not hide the
traces of the runs:

- The health check, `GET /health`.
- A read that the dashboard repeats. The dashboard sends the first read of a
  URL as usual. It marks each later read of the same URL with a `traceparent`
  header whose sampled flag is off. Druks drops that request with its
  statements and calls.
- The database polls of an open stream. The request that opens the stream
  sends one span.

Every caller can mark its own request this way, and Druks then sends no span
for that request. A run always keeps its trace.

With [`log_format = "json"`](#core-process-settings), inside a span that Druks
sends, a log record carries the `trace_id` of the span's trace. A JSON line then
opens its trace in the collector.

A span leaves the process with only its identifiers, its route template, and the
attributes that this section names. Druks removes the rest before the export:

- A request shows its method, its route template, and its status code. Its
  path and its query do not leave.
- A call through httpx shows its method, the host that it reached, and its
  status code. Its URL does not leave, with its path, its query, and its user.
- A statement shows its SQL without the values of its parameters.
- No header and no request or response body leaves.
- An exception shows its type only. Its message and its traceback do not
  leave, because they can quote a request, a response, or the output of an
  agent. A failed run also shows its failure code as `druks.failure_code`,
  for example `timeout` or `rate_limited`. Find the message on the failed run
  in the dashboard, by the `druks.run_id` of the span.

A call through httpx sends only `traceparent`, never the `tracestate` or the
`baggage` of a caller.

## Public URLs and access control

| TOML key | Purpose |
| --- | --- |
| `urls.endpoint` | Browser-visible dashboard URL and MCP OAuth callback base. It is also the `/mcp` base of a box when `urls.mcp_url` and `urls.webhook_host` are empty. WebSocket upgrades must come from its host. When it is empty, they must come from the request's `Host` |
| `urls.webhook_host` | Public webhook hostname and the HTTPS host for this installation's `/mcp` endpoint. A box reaches `/mcp` there when `urls.mcp_url` is empty |
| `urls.mcp_url` | The URL of this installation's `/mcp` endpoint. A box, the calls server, and Druks's own schedule calls dial it, for the Druks tools and the agent browser. When it is empty, the default, they use `/mcp` on the webhook address: `urls.webhook_host`, else `urls.endpoint`. Druks does not start when it is set and is not an absolute `http` or `https` URL |
| `identity.mode` | `none` (default, no authentication, single operator), `header` (edge-asserted identity), or `jwt` (validated edge-signed assertion) |
| `identity.header` | The trusted identity header. The shipped Caddy edge also uses it. Header and JWT modes have no default and require it |
| `identity.jwks_url` | `jwt` mode: where the edge publishes its signing keys |
| `identity.jwt_issuer` | `jwt` mode: required `iss` claim value |
| `identity.jwt_audience` | `jwt` mode: required `aud` claim value |
| `identity.jwt_identity_claim` | `jwt` mode: the JSON Pointer to the account identity in the verified payload (default `/email`) |

The `urls.webhook_host` listener binds every interface. A second TLS
terminator on the same box, for example `tailscale serve` on the tailnet
address, collides with it on port 443. One of the two stops. To keep the
other addresses free, set `DRUKS_WEBHOOK_BIND_HOST` in `[env]` to the public
address. Caddy then serves only that address. To keep IPv6, list the IPv4
and the IPv6 addresses.

`urls.endpoint` and `urls.webhook_host` are different. The first is where an
operator's browser reaches Druks. The second is the public ingress host for
webhook senders. They can share a hostname on exe.dev.

Set `urls.mcp_url` when a box reaches Druks's `/mcp` at an address that the
providers do not reach, such as a private network. Slack, GitHub, Twilio, and
WhatsApp keep the webhook address. The key that a box holds for `/mcp` goes only
to the host of `urls.mcp_url`. The [calls
server](deployment.md#the-calls-server), and Druks itself when it makes a
schedule's tool call, must also reach this address.

The listener at this address must serve only `/mcp`, like the webhook listener.
It must not be the `:8000` dashboard listener or the web port. A box that reaches
either one can send the identity header and act as an operator.

Druks does not authenticate browsers. Identity resolves per request, in this
order:

1. **Personal access token.** If an `Authorization` header is present, it must
   authenticate. A malformed or inactive bearer returns a 401. Druks does not
   continue to another mode.
2. **Header mode (`header`).** The edge authenticates the operator. The edge can
   be exe.dev, Teleport, or Cloudflare Access. It supplies exactly one nonblank
   `identity.header` value. Druks removes outer whitespace and maps the value to
   an account. Druks creates the account at first access.

   The edge controls who can access Druks. Account values are case-insensitive.
3. **JWT mode (`jwt`).** This mode uses the assertion channel from `header`
   mode, but its value is a signed JWT. Druks validates the RS256 signature
   against `identity.jwks_url`. It caches keys for one hour. A token signed with
   an unknown key ID makes Druks fetch the keys again at once.

   The `exp`, `iss`, and `aud` claims must match the configuration. Druks
   resolves `identity.jwt_identity_claim` in the verified payload and maps the
   selected value to an account. A validation error returns a 401 without token
   material. Druks uses a fixed RS256 configuration and does not negotiate it.
4. **No-authentication mode (`none`).** This mode has no authentication or
   identity edge. Druks resolves the only account. Zero accounts is the setup
   state. The first completed provider connection creates the operator account
   from the provider-validated email.

   More than one account is configuration drift. Druks refuses requests and
   startup in this state.

In JWT mode, `identity.jwt_identity_claim` uses
[JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901.html).
Set it in the deployment's `druks.toml`:

```toml
[identity]
jwt_identity_claim = "/traits/email"
```

`/email` selects a top-level claim. `/traits/email` selects a nested claim.
`/people/0/email` selects a claim from the first object in an array.

Dots are literal key characters. Within a key, encode `/` as `~1` and `~` as
`~0`. For example, `/https:~1~1id.example~1email` selects the key
`https://id.example/email`.

Druks refuses invalid pointer syntax at startup in JWT mode. Druks does not
accept bare claim names or dot paths.

The selected value must be a nonblank string. Druks removes its outer
whitespace. A missing path, invalid traversal, or another result shape
returns a 401, and Druks creates no account. The extraction has no
provider-specific behavior.

A subscription is always one person's. An API key is the installation's:
one per provider, owned by no account. Every account sees it in
**Settings → Providers** with the name of the person who last pasted it. A
paste from an admin replaces it. Disconnect clears the secret and retains
the credential identity for historical agent-call billing references.

Before you enable `jwt` mode, make sure that the edge uses the configured header,
claims, and rotation process.

The edge in `header` mode must authenticate each dashboard request. It must
remove client-supplied copies of the configured identity header. Then it must
put the authenticated value in the header. Otherwise, a client can select an
identity. The edge must also terminate TLS and set HSTS.

`jwt` mode has the same header-removal requirement. It also adds cryptographic
provenance. A forged value fails signature validation and returns a 401. Thus,
a bad proxy configuration does not create an impersonated identity.

The shipped Caddy listener is loopback HTTP behind that edge, and the Druks
web listener itself binds loopback by default. In `none` mode there is no
authentication. Keep the listener on loopback. Never publish it.

A public listener that bypasses the identity edge must not forward the
configured identity header. The shipped webhook listener serves only
provider-authenticated `/_external/*`, agents' pictures at `GET /media/*`, and
PAT-authenticated `/mcp` routes. The
issuer listener serves only the `/api/secrets/*` route, which authenticates the
sandbox's identity bearer. These routes do not resolve the header. A future
public listener must keep the same isolation.

Public `POST /_external/*` routes bypass the identity gate and use their own
authentication. Webhooks use signature validation. The notification response
route uses its correlation token. `GET /media/<file-id>` serves
a file only while it is an agent's picture. `GET /api/auth/me` answers without a
resolved account, so that the dashboard can show onboarding in the `none`-mode
setup state.

## Personal access tokens

Agents and other non-browser clients use personal access tokens for the
internal API. Mint these tokens in Settings → API tokens. Send a token as
`Authorization: Bearer <token>`. A token has the form
`druks_pat_<prefix>_<secret>`. Druks stores only the SHA-256 hash of the full
token. It shows the plaintext one time and expires the token after 365 days.

If the header is present, it must authenticate. A bad token returns a 401.
Druks does not use edge identity as a fallback. Token management accepts only a
signed-in identity. It refuses requests that contain an `Authorization` header.
Thus, a leaked token cannot mint or revoke tokens.

If someone compromises a token, mint a replacement. Then revoke the old token in
Settings → API tokens. Revocation is immediate. The list shows the prefix and last
use of each token.

Druks updates last use each hour. Agents consume the API through the MCP
endpoint. See [Connect your agent](connect-your-agent.md).

## GitHub

Druks acts at GitHub as one **operator GitHub App**. This app is its service
identity. The GitHub App receives webhooks and does domain writes such as branches,
pull requests, comments, labels, and merges. Its credentials live encrypted
in Postgres.

Until an operator connects GitHub, agent runs stop with a direct message.
`druks doctor` reports that no GitHub connection exists.

Connect it from **Settings → Connections → Services**. **Create App** starts the
GitHub manifest flow. Enter a GitHub organization, or leave the field empty for
a personal account. Accept the request on GitHub. Druks stores the credentials
and opens the installation page. Install the GitHub App on the applicable
repositories.

Before you create the app, set `urls.endpoint` to the dashboard base URL. If
you set `urls.webhook_host`, the webhook uses that host. Otherwise, it uses the
endpoint host. GitHub refuses a webhook on a local address. Thus, when the
webhook address is not HTTPS, as on a [local install](full-local.md), Druks
creates the App with no webhook and no event subscriptions. GitHub events then do
not reach Druks, and work starts in the dashboard.

You can paste the credentials of an existing GitHub App into the same card.
Enter the GitHub App ID, client ID, client secret, original PEM private key, and
the webhook secret if the App has a webhook. Druks validates the App ID and the
key against GitHub and stores the app slug. Each operator client then uses this
service-identity row. Webhook deliveries use its stored secret for validation.
On a connected card, a secret that you leave blank stays as it is.

The App's slug is the handle that people tag in an issue or pull request comment,
for example `@druks-acme`. A person signs in to Druks through the same App, and
their sign-in links their GitHub account to their Druks account: see
[Chat](chat.md#github). The client ID and secret are the App's OAuth client.
A new paste with the same client ID keeps every linked account. A paste with
another client ID revokes them.

To register the GitHub App manually, use this webhook URL:

Webhook URL:
`https://<webhook-host>/_external/github/events/`

Callback URL:
`<endpoint>/api/oauth/callback`

Subscribe to check run, check suite, issue comment, issues, pull request, pull
request review, pull request review comment, push, repository, status, and
workflow run events. Keep **Expire user authorization tokens** on. Druks
refreshes a person's sign-in with the refresh token that comes with it. Druks
refuses a sign-in that has no refresh token.

| Repository permission | Access |
| --- | --- |
| Metadata | Read |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Checks | Read and write |
| Commit statuses | Read and write |
| Workflows | Read and write |
| Actions | Read and write |
| Repository creation | Write |
| Administration | Read |
| Discussions | Read |
| Dependabot alerts | Read |
| Code scanning alerts | Read |

| Organization permission | Access |
| --- | --- |
| Members | Read |

| Account permission | Access |
| --- | --- |
| Email addresses | Read |

Install the GitHub App on the repositories that Druks will use. This
installation set defines where `software_factory` can act. Personal access
tokens are not a supported substitute.

To change a permission on an existing App, open the App on GitHub under
**Developer settings → GitHub Apps → Permissions & events**. Set the permission,
then save. Each account that installed the App then accepts the change.
`druks doctor` names each permission in these tables that the App lacks.

A sandbox never holds an installation token. It holds a placeholder in
`GH_TOKEN`, and git and `gh` read it through Drukbox's setup. The Drukbox
secrets proxy swaps the placeholder for a token that Druks mints for the
sandbox's repo, with the expiry that GitHub gives it. The proxy fetches a new
token before that token expires. The token reaches that repo and the project's
other repos that the same installation covers, and no other repo.

The `software_factory` build clones and pushes as the operator App. A review
clones as the reviewer App when one is connected. Builds and reviews also get the
GitHub MCP server for `api.githubcopilot.com`. The build's server acts as the
operator App. The review's acts as the review identity.

On each active host, the GitHub card holds the credentials of the operator
GitHub App. Open **Settings → Connections → Services**. Connect GitHub with the
App ID, client ID, client secret, private key, and webhook secret of that App.
Do not create a replacement GitHub App. Its webhook and installations use the
pasted credentials.

When the card has no client ID, **Connect GitHub** fails. Open the card and
select **Replace connection**. Enter the client ID from the App's settings page
and a client secret that you generate there. Leave the other fields blank, and
save. Then add the callback URL to the App on GitHub.

### Repository configuration

A repository configures the `software_factory` build in
`.druks/software_factory/config.yml`. `on_approval` sets what Druks does when a
reviewer approves the pull request:

| Value | Behavior |
| --- | --- |
| `none` | Default. Druks marks the pull request ready for review and ends the build. The repository's own process merges it. Druks records the merge when GitHub announces it. |
| `merge` | Druks squash-merges the pull request, or arms auto-merge when checks still run. |

After each push, the build waits for CI on the head before the review, and red
CI returns the work to the implementer. The pull request leaves draft when the review of
a head with green CI requests no changes, or when a person approves it.

Each agent of a build keeps its conversation for the build. Red CI, a review that
requests changes, and a person's request reach the implementer as the next
message of that conversation, so it does not read the code and its own change
again. This needs Claude Code as the agent's CLI. With another CLI, each round
starts a new conversation.

When Druks receives a push to the default branch that changes `config.yml`, the
next build uses the new file. When a team lists Software Factory, this happens
only for a push by a person who can use Software Factory. Druks refuses a push by
every other author, a merge by the operator App included, and the next build keeps
the old file.

`environment` describes the computer that the commands of a build run on:

```yaml
environment:
  setup: .druks/software_factory/setup.sh
  start: .druks/software_factory/start.sh
  env:
    DATABASE_URL: postgres://localhost/test
  size:
    cpu: 8
    memory_gb: 16
```

| Key | Meaning |
| --- | --- |
| `setup` | Required. A script that installs what the repository's commands need, such as a database server or a toolchain. Druks builds an image from it. |
| `start` | A script that runs each time the environment boots. It starts the services that the commands need to find running, and syncs the dependencies. |
| `env` | Variables that are set when the environment boots. Druks reads them from the repository, so do not put a secret here. |
| `size` | The `cpu` count and the `memory_gb` of the environment. Without it, the environment has the size of the sandbox provider's default. |

Both scripts are files in `.druks/software_factory/`, and Druks reads them from
the default branch, so a pull request cannot change the environment that its own
build uses. Druks builds the image when it profiles the repository, and again
after a push to the default branch that changes a file in that folder. The run
shows **Building sandbox** while the image builds. The profile fails when the
default branch lacks a script that the config names, or when the image does not
build, and the repository keeps its last profile.

The implementer of a build runs the repository's commands in the environment,
with `~/druks-sandbox exec '<command>'` on its own sandbox, and waits for each
one. Druks makes a second sandbox from the image for the implementer, copies the
implementer's checkout to it, with the uncommitted changes, and runs the command
at the root of that copy. The files that the command changes come back into the
checkout. The second sandbox holds no credential: not the model's, not GitHub's,
and not Druks'. It keeps what git ignores, such as installed dependencies,
between the commands of a build, and drukbox removes it when it is idle for 30
minutes. The time that a command takes counts against the timeout of the
implementer.

This version does not run `start`, set `env`, or apply `size` yet.

The same folder holds `review.md`, the repository's rules for the implementer
and the reviewer. The build and review prompts read it when the checkout has
one, and it is the last word where it and a prompt disagree. A section heading
that is a path glob, for example `## frontend/**`, marks the rules for that part
of the repository.

### Review identity (optional)

The bundled `software_factory` app declares an optional service, **Github
Reviewer**. GitHub does not let an App approve a pull request that it authored.
Without a reviewer App, reviews publish as comments from the operator App.
Connect a second App to let reviews approve pull requests and request changes.

Open **Settings → Connections → Services → Github Reviewer** and select
**Create App**. The flow is the same as for the operator App. Install the
reviewer App on every repository that has the operator App. A build hands its
pull request to a review that runs as the reviewer App. When that review fails,
the build fails with the review's reason.

To register the reviewer App manually:

1. In GitHub, open your account or organization settings. Select
   **Developer settings → GitHub Apps → New GitHub App**.
2. Enter a unique App name and your Druks homepage URL. Leave the callback URL
   empty and clear **Active** under **Webhook**. The reviewer receives no
   webhooks and signs in nobody.
3. Set these repository permissions: **Metadata: Read-only**,
   **Contents: Read-only**, and **Pull requests: Read and write**.
4. Create the App. On its settings page, record the **App ID** and select
   **Generate a private key**. Keep the downloaded PEM file.
5. Select **Install App**. Install it on every repository that has the operator
   App.
6. On the **Github Reviewer** card, select **Connect an existing App**. Paste the
   App ID and the full PEM private key. Druks encrypts the private key before
   storage.

See GitHub's [App registration guide](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
for account and organization registration details.

## Ticketing integrations

Select the tracker in **Software Factory → Settings**. The default is Linear.
**none** leaves Software Factory without a ticket tracker.

**Linear** and **Jira** are service identities. Connect them from **Settings →
Connections → Services**. The Linear identity uses an API key and webhook
secret. The Jira identity uses a base URL, email, API key, and webhook secret.
For an Atlassian service account, the email is its address at
`serviceaccount.atlassian.com`.

Druks validates the credentials before it stores them. Linear, Jira, and GitHub
share five status settings: trigger, in progress, in review, done, and resting.
When the selected tracker is connected and lists its statuses, the settings page
offers them as choices. After you change the tracker, save the settings to list
the statuses of the new tracker.

An empty in review or resting status leaves the ticket where it is. An operator
cancel moves the ticket to the resting status only if the ticket is in progress
or in review. A pull request that closes without a merge does the same. The
settings refuse a resting status equal to the trigger status, because that move
starts a new build.

Select **druks** to use the ticket board on this appliance. That choice needs no
credentials. The status settings stay hidden. The trigger status is
Ready for Agent and the done status is Done. Neither is a setting.

Select **GitHub** to take work from GitHub Issues in the repositories that the
operator GitHub App is installed on. That choice needs no second
credential. See [GitHub Issues as the tracker](#github-issues-as-the-tracker).

The dashboard shows the board and the ticket pages only for **druks**. Each
ticket selects a GitHub repository from a Software Factory project. Druks derives
each project's ticket prefix from the project name. It takes the first two
letters and one later letter, and it uses the first prefix that no other project
holds. An identifier is `{prefix}-{n}`. Druks mints it once, and a move to
another repository keeps it.

A ticket that enters Ready for Agent opens a build against the selected
repository. If a scheduled, running, or parked run already exists for that
ticket, Software Factory does not start another.

An agent's `start` tool does not move the ticket to the trigger status. The
build moves the ticket to the in-progress status when it runs.

An agent reads and answers a board ticket through this appliance's own `/mcp`.
The server lists the five ticket tools only while the tracker is **druks**. A
build whose tracker is **druks** asks for that server. You paste nothing and
connect nothing. Druks mints the token of the account that the run belongs to,
and it allows only the five ticket tools.

A comment that the agent writes then carries that person's name. The token
appears in that person's API tokens. Druks mints another once they retire it.

The server uses this installation's [/mcp
address](#public-urls-and-access-control). The box must reach that address. The
agent reads a ticket with `software_factory_get_ticket` and posts with
`software_factory_add_comment`.

The webhook URLs are `/_external/linear/events/` and `/_external/jira/events/`.
The Jira webhook uses a Jira Automation **Send web request** action.

Select **Issue data (Jira format)** as its body. Druks accepts the REST issue
JSON under `issue`. Put the shared token in the `x-druks-webhook-token` header.
Put `{{initiator.accountId}}` in the `x-jira-initiator` header, so Druks knows
who moved the ticket. See [Jira's user smart
values](https://support.atlassian.com/cloud-automation/docs/jira-smart-values-users/).

Druks refuses a delivery without that header with a 400, which the audit log of
the rule shows. `druks doctor` treats a disconnected tracker identity as
optional when that tracker is not selected. It reports pending setup if the
selected tracker is Linear, Jira, or GitHub and its identity is missing.

A ticket at the trigger status opens no build while a ticket that blocks it is
open. On Linear, a blocker is a "blocked by" issue, and it is open until its
state is completed or canceled. On GitHub, a blocker is a "blocked by" issue
dependency, and it is open until the issue gets the done label or closes.
GitHub lists only the dependencies that the installation of the ticket's repo
can read. A blocker in a private repo outside that installation does not hold
the ticket.

On Jira, a blocker is an issue link of
the Blocks type, and it is open until its status category is Done. On the
**druks** board, a blocker is a ticket in the **Blocked by** field, and it is
open until it is Done. When a blocker moves to the done status, Druks reads
each ticket that it blocks. A ticket at the trigger status with no other open
blocker starts its build. On the board, a ticket at the trigger status also
starts its build when an edit of **Blocked by** leaves it no open blocker. No
person starts that build, so it uses the account of the ticket assignee.

On Linear, the planner can split a ticket whose work needs more than one pull
request. It proposes one ticket for each repository, with the "blocked by" links
between them, and a person approves the proposal whatever the plan gate. Druks
then creates each ticket as a sub-issue of the split ticket, at the resting
status and with the label of its repository. It also adds the links. The split
ticket opens no pull request and returns to the resting status.

When the same ticket splits again, Druks reuses the tickets that it created for
it and adds the links that are missing. Druks removes no ticket and no link. A
ticket that the new proposal leaves out still blocks the tickets that it blocked,
until you cancel it.

Software Factory starts a Linear, Jira, or GitHub build under the Druks account
of the person who moved the ticket. If that person has no account and no team
lists Software Factory, the build uses the account of the ticket assignee. If
the ticket has no assignee with an account, the build uses the default account.
If a team lists Software Factory, a move by a person with no Druks account opens
no build.

Linear sends that person in its webhook. Its documentation says that it names no
person only for a deleted person or integration. A build with no person uses the
account of the assignee or the default account, in that order. Jira sends that
person in the `x-jira-initiator` header. GitHub sends the person who added the
label.

A label that the operator App adds counts as a person with no Druks account. A
Linear person has the account of their Linear sign-in. A Jira person has an
account when their Jira ID matches the [provider account](#oauth-grant-identity)
of an MCP connection that they made. A person makes such a connection with
**Connect your account**.

A connection for everyone has no account, so it does not match. A GitHub person
has the account that their [GitHub sign-in](#github) links. When two accounts
match, neither one counts. A **druks** ticket build uses the account of the
ticket assignee.

When a team lists Software Factory, a move by a person who cannot use it opens no
build. Druks logs the refusal and acknowledges the delivery, so the tracker does
not send it again. The work item records nothing.

### GitHub Issues as the tracker

GitHub has no status field, so the GitHub tracker reads the five status settings
as **label names**. An issue that gets the trigger label opens a build. The label
arrives through the operator App's webhook, so the App must subscribe to the
**issues** event. An App made with **Create App** subscribes to it. Add the event
to an App that does not subscribe to it, such as one that you registered by hand.

The status settings default to Linear status names, so name them for GitHub, for
example `ready-for-agent` and `agent:in-progress`. The settings page lists no
labels, so you type them. GitHub creates a missing label with a default color
the first time Druks sets it. The labels act as one status: a label that Druks
sets removes the others, the trigger label too. The done label also closes the
issue.

A label from a person opens a build, and so does a label that Druks sets through
the agent `start` route. A label from another App does nothing. The issue's
repository must be in a Software Factory project, and the build opens its pull
request there. A ticket key is `owner/repo#number`, because issue numbers repeat
across repositories. In a URL path, encode its `#` as `%23`.

## WhatsApp

**Waha** is the service identity that Druks reaches WhatsApp through.
[WAHA](https://github.com/devlikeapro/waha) is an open source WhatsApp HTTP API
that you run beside Druks. Connect it from **Settings → Connections → Services**
with WAHA's address and a key.

Druks uses this key only to create and delete a linked number's session and its
session key. When you run WAHA yourself, the key is WAHA's admin key. Every other
call uses the session key of the number. Druks checks neither value when you save
the card, so you find a wrong key when you link a number.

For deployment-managed WAHA, use [managed services](#managed-services).

Run WAHA with the NOWEB or GOWS engine: Druks takes each reply's message ID from
WAHA before it sends the reply. Druks refuses a number that links on another
engine. WAHA must reach this webhook URL:

Webhook URL:
`https://<webhook-host>/_external/waha/events/`

Set `urls.webhook_host`, or `urls.endpoint`, before you link a number. Druks
writes the URL, the session's webhook secret, and the chats to ignore (status
updates, channels, and broadcasts) into each session's config. Each event
carries an HMAC SHA-512 signature, and Druks refuses an event without a valid
one. See [Chat](chat.md#whatsapp) for how to link a number.

## Slack

**Slack** is the service identity that Druks answers Slack messages as: one Slack
app in one workspace. Create the app from Druks's manifest, paste its keys, and
install it from Druks.

1. Set `urls.endpoint`. Set `urls.webhook_host` when Slack must reach Druks at
   another host.
2. Open **Settings → Connections → Services → Slack** and select **Create Slack
   App**. Slack opens its app creation page, which shows the app of Druks's manifest.
3. Select the workspace, review the app, and create it.
4. On the Slack card, select **Connect Slack** and paste the fields that it
   shows: the signing secret, the client ID, and the client secret. Slack shows
   them on **Basic Information**.
5. Select **Install** on the Slack card. Slack asks you to allow the app in the
   workspace. Druks keeps the bot token, the workspace, and the bot user.
6. In the Slack app's **Event Subscriptions**, verify the Request URL again.
   Slack's first check ran before the card held the signing secret, so Druks
   refused it.

The manifest asks for the bot scopes `chat:write`, `chat:write.customize`,
`channels:history`, `channels:read`, `groups:history`, and `groups:read`. It
also asks for `im:history`, `mpim:history`, `users:read`, `files:read`,
`files:write`, and `reactions:write`. With `files:write`, Druks uploads the
image of a Druks file to Slack: see [Approvals in
Slack](druks-ui.md#approvals-in-slack). The manifest subscribes the bot to
direct messages, to the rooms that it is invited to, and to joins in those rooms.
It leaves token rotation off and names these URLs:

Events URL:
`https://<webhook-host>/_external/slack/events/`

Interactivity URL:
`https://<webhook-host>/_external/slack/interactivity/`

Redirect URL:
`<endpoint>/api/oauth/callback`

Druks checks each event's signature with the signing secret, and refuses an
event older than five minutes. With rotation off, the bot token from the install
and each person's Slack token live until someone revokes them. A person connects
their own Slack account through the same app: see [Chat](chat.md#slack).

The Slack app needs each bot scope and each event of the manifest, and the
Interactivity URL. To give an app what it lacks:

1. Under **OAuth & Permissions**, add each bot scope that it lacks.
2. Under **Event Subscriptions**, subscribe the bot to each event that it lacks.
3. Under **Interactivity & Shortcuts**, set **Interactivity** to on, and set the
   Request URL to the Interactivity URL.
4. Select **Install** on the Slack card again. The workspace then approves the
   app again, with the new scopes.

Druks reads `chat:write.customize` from that install. Until then, an agent's
replies show the Slack app's name, not the agent's name and picture.

While the bot token lacks `files:write`, Slack refuses the upload, and the images
of the message go out as links. After the workspace approves the app again, the
next message or edit shows the images.

## Speech to text

**Speech To Text** is the service that Druks sends voice notes to. It can be a
server that speaks the OpenAI audio API, such as OpenAI, Groq, or a local server.
Connect it from **Settings → Connections → Services** with the server's base
URL, for example `https://api.openai.com/v1`, a key, and a model, for example
`whisper-1`.

Druks checks none of the values when you save the card, so you find a wrong key
on the first voice note. Druks sends a note of at most 25 MiB and refuses a
bigger one without a call. Without the card, the agent tells the person to write
instead. See [Chat](chat.md#whatsapp) for what the agent gets.

## Calls

Druks answers phone calls on your Twilio numbers. Calls need two cards in
**Settings → Connections → Services**:

- **Twilio.** Enter your account SID and auth token. Druks checks them with
  Twilio when you save the card.
- **Voice.** Enter the voice model, its API key, and a voice name if you want
  one. Write the model with its vendor, for example `openai/gpt-realtime-mini`
  or `google/gemini-2.5-flash-native-audio`. A voice name is the vendor's name
  for a voice, such as `marin` or `Kore`. Leave it empty to use the vendor's
  default.

On the **Voice** card, an OpenAI model can also name the transcription model,
which writes the caller's words as text. Leave it empty to use
`gpt-4o-mini-transcribe`. Druks checks only that the model's vendor is `openai`
or `google`, the two that the calls server runs.

When you link a number, Druks sets the number's voice URL in Twilio:

Voice URL:
`https://<webhook-host>/_external/twilio/calls/`

Set `urls.webhook_host` or `urls.endpoint` before you link a number. To link
one, see [Chat](chat.md#calls).

The calls server carries the calls. It is not in the default stack. To add it,
see [the calls server](deployment.md#the-calls-server).

## Harnesses

Druks registers two subscription providers, `anthropic` and `openai`. Each
also accepts an API key. Both connect from **Settings → Providers**. The
connection flow stores each credential in Postgres. Druks refreshes a
subscription token on a schedule.

A sandbox holds a placeholder for the subscription token and never the token.
Drukbox fetches the token from the Druks issuer through the sandbox's identity.
The secrets proxy swaps the placeholder on each request to the entry's host. See
[sandbox identities and the issuer](concepts.md#instructions-harnesses-workspaces-and-sandboxes).

Druks does not copy a host login. Each connection is a capability connection for
the requesting account. In a fresh `none`-mode install, the first completed
subscription connection also creates the operator account. See
[access control](#public-urls-and-access-control).

Anthropic signs in with `oauth`, the login on the Connect page, or with
`setup_token`. A setup token is a token that `claude setup-token` prints, and
you paste it on the Providers page. A setup token lasts one year. It has no
refresh token and carries only the `user:inference` scope. Druks does not
refresh it and shows no quota for it. Druks asks for a new token when it
expires.

An `oauth` login expires on a date that Anthropic sets at each sign-in, about
four weeks later. A token refresh does not move it. The Providers page shows the
date: reconnect before it to start a new login. Three days before the date,
Druks sends the account a notification.

The `[providers.anthropic]` table in `druks.toml` selects the method, `oauth` by
default. An installation on `setup_token` in `none` identity mode gets its first
account from another provider's login, because the paste needs a signed-in
account.

```toml
[providers.anthropic]
sign_in = "setup_token"
```

OpenAI connects through Codex device authorization. Before you connect, open
**Settings → Security** in ChatGPT and enable **device code authorization for
Codex**. In a managed workspace, an administrator must allow it.

A `claude` sandbox reads its placeholder from `ANTHROPIC_AUTH_TOKEN`. With
include apps on, the run starts `claude` with the placeholder in
`CLAUDE_CODE_OAUTH_TOKEN` and the grant's scopes in `CLAUDE_CODE_OAUTH_SCOPES`.
It unsets `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY`. Claude Code then runs
as a subscription login and loads the subscription's connectors. The Chat
adapter starts with the same variables.

A `codex` sandbox reads its placeholder from `CODEX_SUBSCRIPTION_TOKEN`. The
Codex run wrapper writes `~/.codex/auth.json` from that variable before the
command. The file carries the account id, the sentinel refresh token
`druks-placeholder`, and an unsigned id token with the account id, the plan, and
the email. Codex sends the placeholder to `chatgpt.com` on every request.

Codex never refreshes it: the one refresh that it attempts after a 401 fails on
the sentinel, and the turn ends. The real refresh token and id token stay in
Postgres. With include apps off, a Codex run gets `features.apps = false`.

An API key never enters the sandbox. Druks gives the key to Drukbox as a
secret entry when it creates the sandbox. The sandbox holds a placeholder in
the variable that the entry names, and the CLI reads it from the environment. The
Drukbox secrets proxy swaps the placeholder for the key in the entry's header
on each request to the entry's host.

| Harness | Credential | Variable | Host | Header |
| --- | --- | --- | --- | --- |
| `claude` | Anthropic subscription | `ANTHROPIC_AUTH_TOKEN`, moved to `CLAUDE_CODE_OAUTH_TOKEN` with include apps on | `api.anthropic.com` | `Authorization: Bearer` |
| `codex` | OpenAI subscription | `CODEX_SUBSCRIPTION_TOKEN` | `chatgpt.com` | `Authorization: Bearer` |
| `claude`, `pi`, `opencode` | Anthropic API key | `ANTHROPIC_API_KEY` | `api.anthropic.com` | `x-api-key` |
| `pi`, `opencode` | OpenAI API key | `OPENAI_API_KEY` | `api.openai.com` | `Authorization: Bearer` |
| `codex` | OpenAI API key | `CODEX_API_KEY` | `api.openai.com` | `Authorization: Bearer` |

The `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` entries come from the Drukbox
catalog. Druks declares the other entries with their host and header.

The Compose stack runs the secrets proxy on every provider but docker-sbx. See
[the secrets exchange and the secrets proxy](deployment.md#the-secrets-exchange-and-the-secrets-proxy).
Without it, Drukbox refuses the sandbox and the call fails.

**Add provider** searches Models.dev for providers that use one API key.
Druks caches the directory in Redis for one day for search and provider details.
**Save** stores the key and adds the provider and its model list.
When you remove the key, Druks removes the provider.

Provider details show documentation and API URLs from Models.dev.
Druks does not verify provider identity or restrict requests to the listed endpoint.
When an agent runs, OpenCode selects the endpoint.

Before you save a key, check the provider documentation and domain.

Provider rows show the access state, and the weekly quota when Druks has it. Open
**Manage** for the credential controls and the last refresh of the subscription token.
**Manage** also shows the 5-hour quota when Druks has it, and the timestamp of the
model catalog. The weekly quota stays in the provider row.
Anthropic and OpenAI fetch separate model lists.
Added providers use the cached Models.dev directory.

Druks polls subscription usage every five minutes, with intervals up to one hour
while values stay unchanged. An exhausted window waits for its reset unless an
agent call finishes on the subscription. Manual refresh keeps a 60-second
minimum between polls.

An agent call that hits a usage limit waits for the exhausted window's reset, up
to six hours, and then retries once. A reset more than six hours away fails the
call with no wait. If the latest usage reading shows no exhausted window, the
wait is half an hour. If you sign in to another account for the agent's
provider during the wait, the call retries at once. A change to the agent's
model or billing during the wait does the same. If the new account is also
limited, the call waits once more, for that account's reset.

The `claude` and `codex` CLIs run on their own vendor's subscription or key.
`opencode` and `pi` run on an API key only, for Anthropic or OpenAI. OpenCode
uses only the provider of the agent's model.

Druks stores a key for a Models.dev provider, but an agent on that provider
refuses to run. No proven transport carries its placeholder through the secrets
proxy. A model ID is `provider/model` for each harness, for example
`openai/gpt-5.5`. The harness menus disable `opencode` and `pi` until an admin
saves a provider API key.

`paths.harness_config_root` points at optional CLI configuration that Druks
carries into sandboxes. The installer creates the root. Compose mounts it
read-only at `/harnesses`. Claude and Codex each read their named directory:

```text
~/.config/druks/harnesses/
├── claude/
│   ├── .claude.json
│   ├── CLAUDE.md
│   ├── settings.json
│   └── plugins/
└── codex/
    ├── AGENTS.md
    └── config.toml
```

Missing files are optional. Druks copies no credentials file, and it removes the
`mcpServers` block from `.claude.json` before the copy. MCP credentials are
sandbox entries. Provider credentials do not belong in this root. OpenCode and
Pi do not read it.

The default harness, model, billing, effort, and timeout live in **Settings →
Instructions**. Each instruction can override each of them on its app's page.
**Unattended runs use** names the default account.

Shared execution settings select subscription or API key billing. Subscription
billing uses the run account's subscription. API key billing uses the
installation key. If its selected credential is missing, a call refuses before
Druks provisions a VM.

## Sandboxes

| Key | Purpose |
| --- | --- |
| `sandbox.service_url` | Drukbox API base URL. An empty value disables sandbox-backed execution |
| `sandbox.service_token` | Drukbox API token. A [secret](#secrets) |
| `sandbox.timeout` | Control-plane request timeout. The default is 180 seconds |
| `sandbox.image` | Optional provider image override |
| `sandbox.registry_host`, `sandbox.registry_username`, `sandbox.registry_password` | Access to private sandbox images on one registry host, for example `ghcr.io`. Set the three together. The password is the secret [`REGISTRY_PASSWORD`](#secrets-of-an-installation). See [Drukbox](https://github.com/czpython/drukbox/blob/main/docs/deploy.md#private-image-registry) |
| `sandbox.template_repository` | The repository path on that host where Drukbox publishes sandbox templates. The exe provider requires it |
| `[sandbox.host_limits]` | Hosts per provider, one `<provider> = <count>` line each, for example `exe = 28`. A provider not named has no limit. At the limit Drukbox refuses a create, and the run waits for a slot in the phase `queued_for_vm`, up to one host lease |
| `sandbox.proxy_url` | The secrets proxy, at the address that a sandbox dials. The docker shape sets `http://172.17.0.1:8880`. docker-sbx leaves it empty |
| `sandbox.issuer_url` | The issuer base URL that the secrets exchange dials. The default is `http://127.0.0.1:8001`. For a Drukbox on another server, set the address of the Druks host that Drukbox reaches. The installer then serves the issuer route there ([the issuer listener](deployment.md#the-issuer-listener)) |

`DRUKS_SANDBOX_KEYS_DIR` sets the per-host SSH private-key directory. It has
no `druks.toml` key.

`[sandbox].provider` accepts each Drukbox provider name. `docker` selects the
local install shape, `exe` selects the exe.dev + tailnet shape, and every other
name selects the generic remote shape. Provider-specific credentials and host
options live in `[sandbox.<provider>]`, and Drukbox interprets them. See
[deployment](deployment.md) or [full local setup](full-local.md) for the
topology.

## Browser

| Key | Purpose |
| --- | --- |
| `browser.proxy` | Browser egress proxy. A [secret](#secrets). With no value, the browser uses the box IP |
| `browser.proxy_scope` | The launches that use the proxy. `login` (the default) is the login window only. `all` is the login window and every borrow |
| `browser.timezone` | Timezone of the proxied browser (IANA zone). An empty value keeps the container default |

The secret `browser.proxy` sends the browser through an HTTP proxy. The
browser then leaves from a different IP than the box. `browser.proxy_scope`
selects the launches that use the proxy. There are three modes:

- **No proxy.** Leave `browser.proxy` empty. The login window and every
  borrow use the box IP.
- **`login`**, the default. Only the login window uses the proxy. Borrowed
  sessions keep the box IP. Use it for a sign-in flow that refuses a login from
  the box IP. The box IP is sufficient after Druks makes the session.
- **`all`.** The login window and every borrow use the proxy. Use it for a site
  that must see one IP for the login and for each later visit.

If you set the proxy and the exit is not available, the browser fails. It does
not use the box IP in its place.

The value can include a user name and password (`http://user:pass@host:port`).
The browser authenticates the proxy. You do not need an external relay.

Druks does not run the exit. You supply the exit and set this value to it. There
are two common types.

**Your own connection.** Do these steps:

1. Install Tailscale on a home device.
2. Make the device an exit node in the Tailscale app.
3. Add a `tailscale/tailscale` container to the deployment in userspace mode.
4. Set `TS_USERSPACE=true`.
5. Set `TS_OUTBOUND_HTTP_PROXY_LISTEN=:8080`.
6. Set `TS_EXTRA_ARGS=--exit-node=<your-device>`.
7. Add `DRUKS_BROWSER_PROXY=http://172.17.0.1:8080` to the secrets
   section of `.env`.

The browser then leaves from your home connection. The box keeps its own IP for
all other traffic. This exit needs no user name or password. With scope `all`,
scheduled borrows need a running exit device.

**A rented static-residential (ISP) proxy.** First make sure that a detection
service does not already know the IP as a proxy. Then set the secret to the proxy
with its user name and password:
`DRUKS_BROWSER_PROXY=http://user:pass@isp-host:port`.
An ISP IP passes the datacenter-ASN check. A detection service can still find it
and mark it as a proxy.

`[browser].timezone` sets the timezone of the proxied browser. Use an IANA
zone name, for example `Europe/Madrid`. The browser reports a region, and the IP
has a region.

Set both to the same region. Some sign-in flows compare these values. If the
two regions are different, a flow can refuse the login. The timezone applies to
the same launches as the proxy. If you do not set it, the browser keeps the
container default timezone.

## Notifications

Druks sends each notification to one account. The bot of the
[Slack card](#slack) posts it, so connect Slack first. A message that asks for an
answer has buttons. The card's signing secret authenticates the button clicks
that Slack sends back.

Druks sends these notifications:

- **Subscription disconnected.** The provider ended a subscription's login,
  so Druks disconnected it. Reconnect it in **Settings → Providers**.
- **Login expires soon.** An Anthropic login expires in three days or less. The
  notification gives the time in your timezone.
- **Run failed.** A run with no chat failed: no chat started it, or its chat was
  deleted. The notification links to the run's page. A run that still has its chat
  reports its failure in that chat.
- **Run waits for an answer.** A run with a subject, which no chat started, parked
  on a gate. For a gate that you answer in Druks, the notification has buttons
  and a link to the run. A run that a chat started asks in that chat. A question
  that names an [approver](writing-an-app.md#wait-for-input) notifies the approver
  instead, also when another account's chat started the run. A notification that
  Druks cannot deliver does not end the wait or fail the run.
- **Status.** A workflow that no chat started, and that reports its status, keeps
  it in one message that Druks edits as the work continues. See
  [Report status](writing-an-app.md#report-status).

The notification of a run's question or of its failure opens with a card: the
app, then what the run is about, such as a repository or an email. A run with no
subject shows its app alone. When nobody answers a question in time, the run
fails, and its notification says `Failed: Nobody answered the question in time.`

**Settings → Preferences → Notifications** chooses where the bot sends your
notifications:

- **A Slack channel.** The bot must be a member of the channel. If the bot
  leaves the channel later, or the channel is archived or deleted, the bot sends
  the notification to your direct message instead.
- **A Slack direct message.** The direct message is the default. The bot needs your linked
  Slack account. See [Link your account](chat.md#link-your-account).

A run without an account, such as a scheduled run, belongs to the default
account, and that account gets its notifications. The bell in the dashboard
header shows your newest notifications, with or without Slack, except a status.
A notification links to the dashboard only when `urls.endpoint` is set.

## MCP servers

`DRUKS_MCP_CATALOG` points to a JSON catalog of server definitions. Druks loads
this catalog at startup. The packaged catalog contains an empty `mcpServers`
map. Thus, a new installation has no built-in servers. A deployment can point
`DRUKS_MCP_CATALOG` to a mounted file with its defaults.

Druks always loads a catalog. A missing catalog stops startup. Catalogs
contain definitions, not tokens.

`DRUKS_MCP_DIRECTORY` points to the directory of MCP servers that the dashboard
offers to add. The directory is a JSON file that maps each server name to the
server's `title`, `description`, and `url`. The packaged directory lists
vendor-hosted servers:

```json
{"grafana": {"title": "Grafana Cloud", "description": "Dashboards and alerts.", "url": "https://mcp.grafana.com/mcp"}}
```

Every listed server signs in with OAuth. The server must support dynamic
client registration, or a Druks service must own its host, such as GitHub. To
add a server that is not listed, add a custom server.

The dashboard can enable catalog entries and add custom servers. Authentication
is one of:

- Secret headers, which Druks keeps in the vault. A bearer token is the
  `Authorization` header in full. The form's Bearer field composes it.
  **Set key** on the server's row replaces them. A catalog entry with
  `"auth": {"type": "static"}` gets its key the same way. While an enabled
  server has no key, every run fails.
- Secret headers that each person sets. Add the server with **Each person uses
  their own key**. Each account then pastes its own key on the server's row,
  and its runs send that key. A run with no person uses the default account's
  key. Druks does not put the server in the sandbox of an account that set no key.
- An OAuth connection, which requires `urls.endpoint`. The provider redirects
  to `<endpoint>/api/mcp-servers/oauth/callback`. A provider that allow-lists
  redirect hosts, such as Atlassian, must list that URL. **Connect for
  everyone** makes one connection that every run uses. While such an enabled
  server has no connection, every run fails.
- An OAuth connection for each account. **Connect your account** makes each
  account connect its own. Druks does not put the server in the sandbox of an
  account that did not connect it. The run's agent call manifest records the server as
  declared but not delivered.

A connect requests every scope that the server advertises. A provider can
refuse consent for a scope that the organization did not enable. Atlassian
then shows an empty consent page.

**Scopes** on the server's row reads the
advertised list from the server. Untick the scopes that no agent needs and
save. Druks then requests the ticked scopes at each connect, and the row
shows their count. **Use advertised** returns to the full list. If the ticked
scopes miss one that a tool needs, the tool fails with `insufficient_scope`.
Tick the scope and reconnect.

A service can own the host of an OAuth server: GitHub owns
`api.githubcopilot.com`, Jira owns `mcp.atlassian.com`, and Linear owns
`mcp.linear.app`. Such a server uses the service's credential:

- GitHub has sign-ins. Each account uses its own GitHub sign-in, and **Connect**
  opens it. Druks refuses a connection for everyone.
- Jira and Linear hold a pasted login. When the service is connected, the runs
  of the default account send that login. A run that no person starts uses the
  default account, so scheduled runs act as the service's login. Every other
  Jira account connects its own OAuth grant. Every other Linear account uses its
  own Linear sign-in, and **Connect** opens it. Atlassian accepts the Jira login
  only after an organization admin enables API token authentication for its MCP
  server.

Druks gives OAuth discovery and client registration 30 seconds in total.
A timeout names the stage that was pending. Retry the connection.

Druks delivers enabled servers through the selected harness unless an app
workspace declares a server with the same name. Each OAuth bearer and
each secret header is a Drukbox entry behind a vault row. The sandbox holds a
placeholder under a derived variable, and the harness configuration names that
variable. The secrets proxy swaps the placeholder only for the server's host.

The Druks issuer answers the value from the row that Druks bound when it
created the sandbox. A pasted token reaches a running sandbox within five
minutes. A server that someone enables after that gets no entry in a running sandbox. The
API never returns a token.

### OAuth grant identity

At connect and reconnect, Druks records the provider account behind an OAuth
grant. It tries these sources in order:

1. The ID token in the token response. Druks checks its issuer, audience,
   time claims, and nonce. The token comes directly from the token endpoint
   over TLS, so Druks does not check its signature.
2. The userinfo endpoint, if it is on the identity issuer's origin. Druks
   does not follow redirects.

Druks stores the issuer, the `sub` claim, the source, and the name and email
if they are present. It stores `email_verified` only if the provider sends a
Boolean.

`identity_status` is `resolved`, `unavailable` if the provider has no source, or
`failed`. A failed lookup does not change the connection.
`identity_error` records the reason for a failed lookup, such as an HTTP
status or an invalid identity response. It contains no provider response
body or token. The log carries the underlying error. A reconnect records a
new outcome.

The identity comes from the authorization server itself if it offers `openid`.
It comes from the OpenID provider at the origin root if that provider has the
same authorization and token endpoints. If a provider offers `openid`, Druks
adds `openid`, `email`, and `profile` to the requested scopes, and registers
its client with the same scopes.

Druks stores the `scope` field of the token response as the granted scopes.
If the field is missing, the provider granted the requested scopes. If the
field is missing and Druks requested no scopes, the value is `null`. These
facts do not change account ownership or login. Druks uses them to find the
account of a Jira person who moves a ticket, and Software Factory uses them for
a Jira assignee. See
[ticketing integrations](#ticketing-integrations).

## Skills

The dashboard installs skill collections from GitHub repositories. A private
repository needs the GitHub App installed on it.
`DRUKS_SKILLS_DIR` selects the shared writable directory. Its default is
`<DRUKS_DATA_DIR>/skills`.

A call receives the enabled skills that it requests. If it requests none, it
receives each enabled skill. A Software Factory build requests the recommended
set from the repository profile. A call or a chat turn that runs as an agent,
such as a [task run](chat.md#tasks), receives only the enabled skills that its
agent picks. It receives none when the agent picks none.

Druks uploads no other installed skill. The capability manifest records the
delivered set for each agent call.

Before each turn of a conversation with an agent, Druks copies those skills into
the agent's sandbox. An agent on OpenCode gets none. Druks removes from the
sandbox each skill that it copied earlier and that the agent no longer gets. A skill
that an admin removed from Druks is one of them. A chat without an agent gets no
skills.

## Credential custody and secrets at rest

Druks keeps the credentials of providers, services, and MCP servers in one
table, the vault. A vault row has a kind, an audience, and an encrypted mapping
of secrets:

| Kind | Audience | What the row keeps |
| --- | --- | --- |
| `static` | `provider:<id>`, `mcp:<name>`, `service:<slug>` | A pasted API key, one secret header of an MCP server, or the key of a service |
| `static` | `oauth:<…>`, `resource:<…>` | A token that Druks minted, until it expires |
| `app_key` | `service:<slug>` | A GitHub App private key and webhook secret |
| `oauth` | `service:<slug>`, `mcp:<name>` | A refresh token and the client that refreshes it |
| `subscription` | `provider:<id>` | The token payload of a provider subscription |
| `session` | `service:<slug>` | A linked WhatsApp or Twilio number and the secrets of its session |

A revoked row keeps its facts and loses its secrets. An agent call keeps its
reference to the row that it billed. A reconnect revives the row.

The secret `secrets_key` encrypts the vault, the browser-session payloads, and
the secret settings of apps with AES-256-GCM. Each database column supplies
authenticated associated data, and each value gets a derived encryption key. The
setting is one or more comma-separated, base64-encoded 32-byte master keys:

```bash
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

The first key encrypts new values. Each listed key can decrypt values. To rotate
the key, put a new key first in `.env`. Then run `docker compose up -d`:

```bash
DRUKS_SECRETS_KEY=<new>,<old>
```

While a stored row depends on the old key, keep that key. If you lose each key
for a row, you cannot recover that secret. Reconnect the OAuth grants and the
subscriptions. Enter the static tokens again. Log in to the affected browser
sessions again. Validation and API errors do not include submitted secret
values.

`SECRETS_KEY` in `.env` encrypts the secret entries of each sandbox in the
Drukbox database. The installer generates it for the Drukbox API and the secrets
exchange. Rotate it as you rotate `secrets_key`, with the new key first.

GitHub App private keys, the operator identity's and the review identity's,
are encrypted vault rows.
