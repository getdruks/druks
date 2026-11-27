---
title: "Druks UI"
description: "The V1 contract for server-driven app pages: declarations, blocks, values, fields, actions, and liveness."
sidebarTitle: "Druks UI"
icon: "layout-dashboard"
---

A Druks app declares its screens in Python. It returns typed `Page` objects
from `pages.py`. The shared dashboard renders them. It also resolves
navigation, runs actions, and refreshes followed regions.

Most apps write no JavaScript. An app that needs full control of its interface
ships an ESM frontend. See [frontends](writing-an-app.md#frontends) for that
path.

This page is the V1 contract. It gives the exact Python fields and the exact
JSON for every public model. The backend, the renderer, and
[the gallery](https://github.com/getdruks/druks-ui-gallery) implement this
page. Nothing else names these shapes.

## Terms

The contract uses eight terms. Each one has one meaning.

| Term | Meaning |
| --- | --- |
| `Page` | One screen. A page function returns it. |
| `Block` | One piece of a page. Blocks nest. |
| `Value` | One rendered datum inside a block. |
| `Field` | One named input. An action collects it before it runs. A page filter collects it in the URL query. |
| `Action` | A control that calls one of the app's operations. |
| `Link` | A control that navigates. |
| `operation` | The `operation_id` of an app route. |
| `arguments` | The fixed values that an `Action` or a `Link` carries. |

## Import surface

Every public name comes from `druks.ui`:

```python
from druks import ui
```

`druks.ui` imports no app. It is a platform namespace like `druks.workflows`.

It exports exactly these names:

```text
page                                       declaration
Page  Follows                              the snapshot
Text  Markdown  Quote  Section  Card       display and layout blocks
Cards  Callout  Divider  EmptyState
Stack  Columns
Link  Action  Form                         controls
Timeline  TimelineItem  Progress           run and artifact blocks
ProgressStep  Image  Files
FileSummary  GateControls
Chart  ChartSeries  ImageGallery           rich data blocks
Metrics  Metric  Facts  Fact  Table
TableColumn  TableRow  List
TextValue  NumberValue  StatusValue        values
SubjectStatus  TimeValue  ControlsValue
Option  TextField  TextAreaField           fields
NumberField  SelectField  MultiSelectField
RadioField  CheckboxField  UploadField  MultiUploadField
SecretField
Block  Value  Field                        the three unions
```

## Declare pages

Pages are in `pages.py`. Druks discovers that module the way it discovers
`routes.py`.

```python
from druks import ui


@ui.page("/")
async def overview():
    return ui.Page("Overview", blocks=[ui.Text("Peers this install tracks.")])


@ui.page("/peers")
async def peers(): ...


@ui.page("/peers/{peer_id}")
async def peer(peer_id: int): ...


@peer.child("/history")
async def peer_history(peer_id: int): ...
```

`peers` and `peer` are two top-level pages. `peer_history` is a child of
`peer`. A page path can hold as many segments as the app needs. The one-level
rule counts `child` declarations, not path segments.

A page function needs no return annotation. The route names `Page` as its
response model. An annotation on each function adds nothing.

Each page has a name. The name is the function name. `Link` and
`App.navigation` reference a page by that name.

A subject detail page can be the destination for Dashboard decisions:

```python
@ui.page("/peers/{peer_id}", subject=Peer)
async def peer(peer_id: int): ...
```

The route must have exactly one parameter. Druks fills it with the subject ID.
An app can declare one decision page for each subject type.

The dashboard keeps the requested run and decision round in the URL. The page
shows controls only for that run. A decision that changed or does not exist shows
an unavailable message. When the operator answers on the page, the page removes
the run and round from the URL. Ordinary subject links still open the platform
subject page.

Each page has a label. Druks derives the label from the name. Underscores
become spaces. `peer_history` becomes "peer history". Pass `label=` to
override it:

```python
@ui.page("/peers", label="Peer roster")
async def peers(): ...
```

### Rules

Druks checks these at boot. A broken rule raises an error with the app name and
the exact cause.

- At most one page declares `/`. That page is the landing page. An app that
  declares none gets the platform home. The home shows one live table for each
  subject type. Its columns are the summary's own fields and where the work on
  each subject stands. An app whose home is a React route declares none.
- `@page` declares a top-level page.
- `@parent.child` declares a child page.
- Druks allows one child level. A child of a child is a boot error.
- A child declaration can be in another module.
- A child inherits every parameter of its parent route.
- An extra child parameter must come from the relative child path.
- A page function takes one required parameter for each parameter of its route.
  Extra parameters must have defaults. FastAPI binds them from the query string
  as filters. Each parameter must be callable by name. A positional-only or
  variadic parameter is a boot error.
- A catch-all is the last segment of its route. A catch-all anywhere else
  matches every route under it. It is a boot error.
- A static child is a tab. The parent is the first tab.
- A parameterized child is not a tab. A `Link` opens it.
- A parameterized detail page shows a link back to its parent.
- Tab order is the parent, then the static children in declaration order.
- Declaration order does not control route matching.
- Two pages in one app with the same name are a boot error. `Link` and
  `App.navigation` both address a page by name.

The app roster at `GET /api/apps` carries the page table. The shell resolves a
`Link`, a tab strip, and a parent link against it:

```json
{
  "pages": [
    {"name": "overview", "label": "overview", "path": "/night_watch", "parent": "", "subjectType": "", "order": 0},
    {"name": "peers", "label": "peers", "path": "/night_watch/peers", "parent": "", "subjectType": "", "order": 1},
    {"name": "peer", "label": "peer", "path": "/night_watch/peers/{peer_id}", "parent": "", "subjectType": "", "order": 2},
    {"name": "peer_history", "label": "peer history", "path": "/night_watch/peers/{peer_id}/history", "parent": "peer", "subjectType": "", "order": 3}
  ]
}
```

The table arrives in route-match order. A renderer that mounts a route for each
entry in turn then matches a literal segment before a parameter. `order` is
the page's place in the app's declarations, which is the order that its tabs
show in. Route matching sorts the table. Only `order` then keeps the order of
the declarations.

`path` is the shell path, not the API path. The shell fills each `{name}`
placeholder from the `Link` `arguments` and percent-encodes the value.
`arguments` values are strings. FastAPI coerces each one to the type that the
page declares. A `Link` without one of its arguments is broken.

A page function can take extra parameters with defaults. Those are query
filters. The shell keeps their values in the URL and sends them on every page
read. The shell keeps `run` and `parkedAt` for itself. They name a parked
decision, not a filter.

```python
@ui.page("/peers")
async def peers(status: str = ""):
    return ui.Page(
        "Peers",
        filters=[
            ui.SelectField(
                name="status",
                label="Status",
                options=[
                    ui.Option("Any", value=""),
                    ui.Option("Live", value="live"),
                ],
                value=status,
            )
        ],
        blocks=[...],
    )
```

The parent of a page is its `parent` entry when it has one. Otherwise it is the
declared page whose `path` is the longest proper prefix of this page's `path`.
When no other page is a prefix, it is the landing page. A parameterized detail
page links to that parent.

### Navigation

`App.navigation` is a flat, ordered list of page names:

```python
class NightWatch(App):
    name = "night_watch"
    navigation = ["overview", "peers"]
```

Each entry names a static top-level page. The shell shows the page label. A
navigation entry declares no second label.

These are boot errors:

- A name that no page declares
- A parameterized page
- A child page.

The app roster at `GET /api/apps` carries the resolved pairs. The shell needs
no second read:

```json
{"navigation": [["/night_watch", "overview"], ["/night_watch/peers", "peers"]]}
```

An app that ships an ESM frontend declares its own tabs inside that frontend.
`App.navigation` names declared pages and nothing else.

## Routes

Druks builds the full table of page routes before it registers any route with
FastAPI. It sorts the table so that matching is global and not declaration
ordered.

A path segment has one of three kinds. The sort key of a segment is its kind:

| Kind | Example | Key |
| --- | --- | --- |
| literal | `peers` | 0 |
| parameter | `{peer_id}` | 1 |
| catch-all | `{rest:path}` | 2 |

Druks compares two paths segment by segment. A literal segment comes before a
parameter. A parameter comes before a catch-all. The rule applies at every depth,
for top-level pages and for child pages.

`/peers/new` thus always matches before `/peers/{peer_id}`, whichever one the
app declares first.

Two page paths have equivalent parameter shapes when they are equal after
Druks replaces every parameter name with a placeholder. `/{id}` and `/{slug}`
are equivalent. That is a boot error. A request cannot select one of the two.

### The page API

Druks mounts one route for each page under the app's namespace:

```text
GET /api/<app>/pages<page path>
```

The path of the landing page has no trailing slash:

```text
GET /api/night_watch/pages
GET /api/night_watch/pages/peers
GET /api/night_watch/pages/peers/7
GET /api/night_watch/pages/peers/7/history
```

The endpoint is the page function. FastAPI validates each path parameter
against the declared signature. The route answers 422 for a value that the
declared type rejects. The route is behind the dashboard identity gate, like
every other `/api/<app>` route.

The shell reads a page at `/<app><page path>`. It calls the page API route
that matches.

`pages` is a reserved segment under `/api/<app>`, with `transcripts` and
`uploads`. A subject type with one of those names is a boot error. An app router
whose prefix is one of them is a boot error. Without the check, FastAPI hides
one of the two by registration order.

## Page purity

A page function is a pure read-side projection.

A page function can do these things:

- Read Druks state
- Read the app's own data
- Read a projection
- Read a read-only external source.

A page function cannot do these things:

- Write data
- Start or enqueue work
- Publish an event
- Answer a gate
- Cause an external effect
- Depend on mutable process state.

Druks reruns a page function on the first load, on an event, on a reconnect, on a
manual refresh, and on a retry. Druks guarantees neither the call count nor the
call order. Write the function so that a second call costs nothing.

## Liveness

A `Page` or a named region declares what it watches:

```python
@ui.page("/peers/{peer_id}")
async def peer(peer_id: int):
    watched = await Peer.get(id=peer_id)
    return ui.Page(
        title=watched.name,
        blocks=[
            ui.Section(
                name="decision",
                title="Decision",
                follows=watched,
                blocks=[ui.GateControls(watched)],
            )
        ],
    )
```

A named region is a `Section` with a `name`. The name must be unique in the
page.

`follows=` takes the subject that the page watches. Druks reads
`subject.identity` and fills `subject_type` and `subject_id`:

```json
{"subjectType": "peers", "subjectId": "7"}
```

A run is always about a subject. The stream carries the subject. A region that
watches a run follows that run's subject. The page function already read that
subject to render the page.

A `Section` that follows a subject must have a `name`. The shell replaces the
region by name. It can never replace an unnamed one.

Druks reuses the per-subject event stream that every app gets:

```text
GET /api/<app>/<subject type>/<subject id>/stream
```

`follows=` also takes the subject class. The page or the region then watches
every subject of that type. `subject_id` is empty. The shell reads the board
stream:

```text
GET /api/<app>/<subject type>/stream
```

A page that shows many subjects is live this way.

On a `snapshot` event the shell reads the page again. It takes the named
region from the new page and replaces that region in full. It sends no block
diffs.

The shell keeps scroll position, focus, and unsubmitted form values outside
the region.

The shell owns the `EventSource`, the reconnect, the retry, and the
stale-response protection. A response from an older read never replaces a
newer one.

A `follows=` on the `Page` itself replaces the whole page body. When the page
follows one subject, the shell also links it to that subject's own page. The
timeline of every run is there.

## Gates

`GateControls` takes the subject that waits on the operator:

```python
ui.GateControls(peer)
```

The shell shows nothing while the subject waits on nothing. Otherwise it shows
the questions, the options, the recommended choice, the context, the controls,
the note, and the artifact. The note box shows only when the gate declares
`note`.

- The shell reads `GET /api/gates/{run}`.
- The shell submits `POST /api/gates/{run}/answer`.
- The answer echoes `parkedAt` unchanged. Druks rejects a stale `parkedAt`.
- Both routes use the signed-in dashboard session through `current_account`.

`GateControls` is not an `Action`. It never calls `/api/runs/{run}/resume`.

A `GateControls` block must be inside a `Page`, or inside a named `Section`,
that follows a subject. Druks rejects a `GateControls` block with no such
ancestor when it builds the page. Without the follow, an answered gate stays
on screen.

When the operator answers, the followed region refreshes. It then shows no controls.

## Where the work stands

`SubjectStatus` shows where the work on one subject stands:

```python
ui.TableRow([target.name, ui.SubjectStatus(target, working="auditing")])
```

When Druks serves the page, one read gets the status of every subject on it,
whatever their types. The status comes only from the runs of the app that serves
the page. A run of another app on the same subject does not show. The shell writes
the word:

- "needs you" while the subject waits on the operator
- "failed", "cancelled", or "orphaned" when the work stopped, with its message
- The phase of the run while it starts or waits, such as "Waiting for a sandbox
  slot…", in the words of the run page
- The `working` word while a run of the subject is scheduled, or while its agent
  works
- "idle" otherwise.

`subjects()` on a block or a page gives the `(subject_type, subject_id)` of every
`SubjectStatus` and `GateControls` inside it, at any depth. Druks gets the status
of each one in that read.

Druks reads a `SubjectStatus` in a review's
[context of blocks](writing-an-app.md#wait-for-input) in the same way, from the
runs of the app that asks. It reads the status each time it reads the ask: for
the dashboard, for `get_gate`, and for a chat agent. It reads the status once for
the approval, when the run parks. The ask that Druks stores, and the event that
records the ask, hold no status. A recorded request on the Activity page then
shows none.

## Text forms

Every block and value except `GateControls` has a text form. An `Action` and a
`Form` have one only in a review's context. `to_markdown()` takes no arguments.
It writes the text form as markdown that names no channel.

An approval keeps the blocks of a review's context beside their text form. A
channel that draws no blocks sends the text. Slack draws some of the blocks with
its own blocks. See [Approvals in Slack](#approvals-in-slack).

| Block or value | Text form |
| --- | --- |
| `Text`, `Markdown` | The text as written. |
| `Quote`, `Callout` | A block quote. The title of a callout is in bold. |
| `Divider` | A rule. |
| `Section`, `Card`, `Cards`, `Stack`, `Columns` | The blocks inside, in order. A title becomes a heading. |
| `Facts`, `Metrics` | A list: each label in bold, then its value. |
| `List` | A list. |
| `Table` | A markdown table. The row detail stays on the dashboard. |
| `Chart` | A markdown table of its numbers: a row for each category and a column for each series. |
| `Progress` | A list of its steps with the status of each. Without steps: the share as text, or that it still runs. |
| `Timeline` | A list of its items, oldest first, with the time of each. |
| `Link` | A link to the page on the dashboard, or to the outside address. |
| `Image`, `ImageGallery`, `Files` | A link to each image or file. |
| `EmptyState` | The title and the description. |
| `TextValue`, `StatusValue` | The words. A value with a link is a link. |
| `NumberValue` | The number with its unit. |
| `TimeValue` | The time in UTC. |
| `SubjectStatus` | The word that the dashboard shows. |
| `ControlsValue` | Its links. In a review's context, its actions too. |

A link is a link in text when its `url` holds its address. A link to a page or
to a subject names it inside one app. On its own, such a link shows only its
words.

For a review's context, Druks gives each of these links its address from
the pages of the app that asks, before it writes the text. A link to a page
opens on the dashboard at `urls.endpoint`, with its arguments in the route. A
link to a subject opens on the page that the app declares for the subject's
decisions, else on the subject's own page. A path such as the `/api/files/…`
address of an image also uses the endpoint. An installation with no endpoint
has no address for the dashboard. Such a link then shows only its words.

A link to an outside `http(s)` address stays a link. Any other address, such as a
`data:` image, shows only its words. Druks escapes a bracket in the words of a
link. It encodes a space or a parenthesis in the address. The link then stays a
link.

`GateControls` has no text form, because the operator acts through it. Its
`to_markdown()` raises `NoTextFormError`, which names the block. `review` refuses
a context that holds one anywhere, before the workflow waits for an answer.

An `Action` has a text form only in a review's context. When `review` stores
the context, Druks gives each action the page of the run. That page shows the
action as a button. The text form of the action is its label, linked to that
page. An installation with no endpoint has no address for the page. The action
then shows only its label.

A table that selects rows writes its actions in the same way. So do a `Cards`
list with a `drop` action and a block with an action in its controls.

A `Form` in a review's context gets the page of the run on its action and on each
extra action too. Its text form is its title as a heading, its description, and
the label of each of its actions, linked to that page. A channel that shows no
inputs then shows the form as a link to the page where the person completes it.
Slack draws some actions and forms with inputs and buttons. See
[Actions in Slack](#actions-in-slack).

In any other place, the `to_markdown()` of an `Action` or a `Form` raises
`NoTextFormError` too. `report` refuses a [status](#status-in-slack) that holds
an `Action`, a `Form`, or a `GateControls`, before it saves the status.

## Approvals in Slack

Slack draws these blocks of an approval's context with its own blocks.
[Cards and groups in Slack](#cards-and-groups-in-slack) says which of them Slack
draws inside another block:

| Block | In Slack |
| --- | --- |
| `Facts`, `Metrics` | A grid of two columns: each label in bold above its value, under the title in bold. A section of Slack holds 10 of them. More start a new section. |
| `Divider` | A divider. |
| `Link` | A button that opens the address of the link. |
| `Table` | A data table that the person can sort and read page by page. The title is its caption, or the first column label when the table has no title. A `NumberValue` is a number cell. Slack sorts a column of number cells as numbers. A value with a link is a link. A cell that holds only an `Action` with no fields is a button. See [Actions in Slack](#actions-in-slack). Any other value shows its text form. An empty value or column label shows a dash. The row detail stays on the dashboard. A table that selects rows shows its text form. |
| `Chart` | A chart of the same kind: line, bar, area, or pie. A line, bar, or area chart has the categories along the axis, in their order. Each series has a point for each category. The category label and the value label are the titles of the axes. A pie has no axes. It has a segment for each category, in their order, with the label of the category and its value. A chart with no title takes its value label as its title. An empty category or series label shows a dash. |
| `Progress` | With steps, a plan of tasks under its label, with a task for each step. The tone of the status of a step gives the state of its task. See [Plans in Slack](#plans-in-slack). |
| `Timeline` | A plan of tasks under its title, with a task for each item, oldest first. The description and the time of an item are the details of its task. See [Plans in Slack](#plans-in-slack). |
| `Image` | An image. Slack fetches an image at an outside address. Druks uploads the image of a Druks file to Slack. The alternative text is its alt text. The caption is its title. An image with no caption has no title. |
| `ImageGallery` | An image for each image of the gallery, in their order. The title of the gallery is a heading above them, as in its text form. A gallery with no title has no heading. |
| `Card` | A card with its title, its description, its image, and a button for each link. A card that holds other blocks is a container. |
| `Cards` | A carousel of cards that the person can scroll, under the title of the list. |
| `Section` | A container under its title. A section with no title shows its blocks in order. |
| `Columns` | A layout with the columns side by side. |
| `Stack` | Its blocks, in order. |
| `Action` | At the top of the context, an input for each of its fields. Under them, a button runs the action as the person who clicks it. See [Actions in Slack](#actions-in-slack). |
| `Form` | At the top of the context, a form that sends on a button has an input for each of its fields. Under them, it has a button for its action and one for each of its extra actions. See [Actions in Slack](#actions-in-slack). |

Every other block shows its text form. A value in a fact or a metric shows its
text form. A value with a link stays a link. Slack has no colored label. A
`StatusValue` shows its words without its tone. In a
[plan](#plans-in-slack), the tone of a status gives the state of a task.

A click on a link button opens the address. The click does not answer the
approval.

A block past a limit of Slack shows its text form:

- A fact or a metric of more than 2,000 characters, with its label in bold on a
  line of its own
- The title of a `Facts` or a `Metrics` of more than 3,000 characters, in bold
- A link or an action label that is empty or that has more than 75 characters
- A link address of more than 3,000 characters
- An action that asks to confirm, when its label is more than 30 characters or
  its `confirm` is more than 300
- An image address of more than 3,000 characters, after Druks encodes it
- The alternative text or the caption of an image, of more than 2,000
  characters
- An action or a form with an input past a limit of Slack.

In a table, an action past one of these limits shows its text form in its cell.
[Actions in Slack](#actions-in-slack) gives the limits of an input.

A link with no address shows its words, as in text. A message holds 50 blocks,
its buttons included. When the message has no room for more blocks, the other
blocks show their text forms in the last block.

The markdown of a message holds 12,000 characters. When there is more, Druks cuts
its end. The last line then says that Druks cut the end. It links to the run's
page when the installation has an endpoint.

A data table holds 200 rows under its header, and 20 columns. The cells of all
the data tables of a message hold 20,000 characters, the column labels included.
Druks counts each cell by its text form, a button by its label, and an empty
cell by its dash.

Druks keeps the first rows of a table that fit. A line above the table then says how
many rows Druks did not send. It links to the run's page when the installation has
an endpoint. A table shows its text form when it has no columns, more than 20
columns, or no rows, or when no row fits.

A message holds 2 charts. The first 2 charts that Slack can draw show as
charts. Each chart after them shows its text form. Slack requires a title. A
chart with no title and no value label shows its text form. Druks cuts a title
or an axis title to 50 characters.

A line, bar, or area chart holds 12 series of 20 points. Druks cuts a category
or a series label to 20 characters. Such a chart shows its text form when it has
no series or no categories, more than 12 series, or more than 20 categories. It
shows its text form too when the cut gives two categories or two series the same
label.

A pie holds 12 segments, one for each category. A segment label holds 20
characters. Druks cuts no label of a pie. A cut label can hide the category of a
share. A pie shows its text form when it has no categories, more than 12
categories, or a category label of more than 20 characters.

Slack draws a png, a jpg, or a gif. Slack fetches an image at an outside
address. Druks does not fetch it. Druks knows its format only from the address.

Such an image shows as an image when its `url` is an outside `http(s)` address.
The path of the address must end in `.png`, `.jpg`, `.jpeg`, or `.gif`, in
capitals or not. The query and the fragment do not count. Slack fetches the
address that the text form links. Druks encodes a space or a parenthesis in it,
as in text.

Slack cannot read a Druks file by its address. Druks uploads the file to Slack
instead. An image is a Druks file when its `url` is the `url` of a `File`,
`/api/files/{id}`, alone or after `urls.endpoint`. The same path on another host
is an outside address.

Druks reads the format from the content type of the file: `image/png`,
`image/jpeg`, or `image/gif`. It uploads a file of at most 10 MiB, and only when
the message has room for the image. Druks uploads the file again for each message
and for each edit of a message. It deletes no file that it uploads.

Slack keeps the uploaded file private: Druks shares it in no channel. The image
block shows it to each person who sees the message. Druks uploads a file only
when the account that the message is for can use the app of the file. A download
from Druks requires the same. Druks does not check each person in the channel.

The upload needs the bot scope `files:write`: see [Slack](configuration.md#slack).

Every other image shows its text form:

- An svg, or an address with no extension.
- Every other path on the dashboard, and a `data:` address.
- A Druks file of another type, or of more than 10 MiB.
- A Druks file of an app that the account of the message cannot use.
- An id that names no file, and a deleted file.

A Druks file whose upload fails shows its text form too. The message still goes
out. When Slack answers that the app lacks `files:write`, Druks uploads no other
image of that message. The next message tries again.

A `Files` block shows its text form: a link to each file. Druks uploads none of
its files. Each download then stays behind the sign-in of Druks.

Slack draws each image of a gallery that it can draw. Each other image keeps its
line of the text form, in its place among the images. A gallery with no image
that Slack can draw shows its text form. A message holds all the blocks of a
gallery or none of them. When the message has no room for all of them, the
gallery shows its text form in the last block, with the blocks after it. The
heading and the lines of a gallery count in the 12,000 characters of markdown.

When Slack refuses the blocks, Druks sends the text of the approval in the same
place, with the same buttons. Slack refuses them, for example, when it cannot
download an image, or when the channel takes no images. Slack also refuses a
file for a moment after its upload. When Slack refuses a file, Druks posts the
blocks once more, about a second later, before it sends the text. A table or a
chart then shows as a markdown table, and an image as a link. An action or a
form shows its text form.

Druks edits the message when the approval gets an answer or closes. The edit
draws the blocks again, with the outcome in place of the buttons. An action and a
form then show their text forms.

### Cards and groups in Slack

A `Card` that holds only `Link` blocks and one `Image` at most is a card of
Slack. The card shows the title, the description as its body, and the image as
its hero image. It has a button for each link: the `link` of the card, each
`Link` among its blocks, then each `Link` of its controls. The button for the
`link` of the card shows the title, as in its text form. A card with no title
shows no button for its `link`.

Any other `Card` is a container. The container shows the title, the
description as its subtitle, and the blocks of the card. The buttons for the
`link` of the card and for each `Link` of its controls follow the blocks. A
`Section` with a title is a container too, under its title. The buttons for the
`Link` controls of a section come before its blocks.

Slack runs no action of the controls of a card or a section. An `Action` of the
controls does not show in Slack. The drag of a card and the `drop` action of a
`Cards` list stay in the dashboard.

A `Cards` list is a carousel when each of its cards is a card of Slack. A
carousel holds 1 to 10 cards. Otherwise each card of the list shows on its own,
in order. The card is then a card of Slack, a container, or its text form. The
title of the list is a heading above the cards. An empty list shows its text
form: its title and its `empty` state. Each `layout` of a list follows these
rules: `wrap`, `stack`, and `tiles` show alike.

`Columns` of 3 blocks or fewer are a layout of Slack, with the columns side by
side, in order. More than 3 columns show one under the other, as a `Stack`
does. The columns of a layout have the same width for each `layout`: `even`,
`sidebar`, and `split`. A carousel, a data table, and a chart each take a row
of the layout. The other columns share a row. A layout holds 3 items in a row
and 5 rows. Slack requires a label for a layout. Druks gives it the label
`Columns`, which Slack does not show.

A `Stack` and a `Section` with no title show their blocks in order, in their
place. The buttons for the `Link` controls of the section come first.

Slack holds only some blocks inside a container or a layout:

- A container holds `Text`, `Facts`, `Metrics`, a `Divider`, a `Link`, an
  `Image`, and `Columns`. Text is a section of Slack's mrkdwn. Druks escapes
  `&`, `<`, and `>` in it.
- An item of a layout holds the same blocks, except `Columns`. It also holds a
  `Card`, a `Cards` list with no title that is a carousel, a `Section` with a
  title, a `Table` that selects no rows, and a `Chart`.
- A `Stack` and a `Section` with no title hold the blocks that their place
  holds. They add no level.
- Slack puts a container or a layout in another one 2 levels deep at most.
- Each column of a layout is one block. A column that shows more than one block
  makes the columns show their text form. Examples are a stack of two blocks,
  and `Facts` of more than 10 facts.

A group that holds any other block shows its text form. So does a group with a
block past a limit of Slack, or a block that Slack cannot draw. The text form is
a markdown block at the top of the message, in place of the whole group. Slack
takes no markdown inside a container or a layout. Druks thus shows no part of
such a group with Slack's blocks.

A group shows its text form past one of these limits of Slack:

- The title of a card of more than 150 characters, a description of more than
  200 characters, or more than 3 links
- The title or the subtitle of a container of more than 150 characters, no
  title, or more than 10 blocks inside
- Text inside a container or a layout of more than 3,000 characters
- A table in a layout whose rows do not all fit. Only the top of the message
  holds the line that says how many rows Druks did not send.

Under a `Card`, a `Cards` list, a `Section`, `Columns`, or a `Stack`, an action
and a form show their text forms. A cell of a table there shows the text of its
action. See [Actions in Slack](#actions-in-slack).

Each block inside a container, a carousel, or a layout takes one of the 50
blocks of a message. When the message has no room for a whole group, the group
shows its text form in the last block, as other blocks do. A table and a chart
inside a layout use the room of the message for its data tables and charts. A
group that shows its text form takes none of that room. Its text counts in the
12,000 characters of markdown.

The image of a card is its hero image. Slack shows no caption on a hero image.
Druks uploads the image of a Druks file to Slack for a card, a container, and a
layout too, with the same checks as for an `Image` block. Each file goes up once
for a message, however many times the message shows it. Druks uploads no image
of a group that shows its text form. When Slack cannot show the image of a
card, the card shows its text form, with the link to the image. A list of cards
then shows each of its cards on its own.

### Plans in Slack

A `Progress` with steps is a plan of Slack, with a task for each step, in their
order. The label of the progress is the title of the plan. A task of Slack is
pending, in progress, complete, or in error. The tone of the status of a step
gives the state of its task:

| Tone of the step | Task |
| --- | --- |
| `active` | In progress |
| `success` | Complete |
| `danger` | In error |
| `neutral`, `warning` | Pending |

A task does not show the words or the link of its status. After a person
cancels a workflow, the step of its [status](#status-in-slack) that was in
progress has the tone `neutral`. Its task is then pending, above the callout
that says "Cancelled.". A `Progress` with no steps shows its text form: its
share, or that it still runs.

A `Timeline` is a plan with a task for each item, oldest first. The title of the
timeline is the title of the plan. A timeline with no title gets the title
"Timeline". The description of an item and its time in UTC are the details of
its task. An item happened, so its task is complete. When the status of the item
has the tone `active` or `danger`, the task takes the state of that tone. A
timeline with no items shows nothing, as its text form does.

A plan holds 50 tasks. A longer plan keeps the last 50 tasks. A line above the
plan then says how many tasks Druks did not send. It links to the run's page
when the installation has an endpoint.

Slack takes one plan in a message. The first `Progress` with steps or
`Timeline` with items is the plan. Each later one shows its text form. Slack
holds a plan only in the message, not in a container or a layout. A `Stack` and
a `Section` with no title show a plan in their place. A group that holds a
`Progress` or a `Timeline` shows its text form. A plan takes one of the 50
blocks of a message, whatever the number of its tasks.

### Actions in Slack

An approval in Slack draws an `Action` and a `Form` at the top of the review's
context. Each field of the action or the form is an input. Under the inputs, each
action is a button:

```python
reply = await self.review(
    context=[
        ui.Facts([ui.Fact("Body", note.body)]),
        ui.Action(label="Archive", operation="archive_note", arguments={"note_id": note.id}),
        ui.Form(
            fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
            action=ui.Action(label="Write a note", operation="write_note"),
        ),
    ]
)
```

An action with no fields that is alone in a cell of a table is a button too. The
table must be at the top of the context and select no rows. A block inside a
`Card`, a `Cards` list, a `Section`, `Columns`, or a `Stack` is not at the top of
the context.

The `operation` of each action in the context names a write of the run's own
app. `review` checks the operation when the run parks, as a page read checks it.
A button shows the `label`. With `confirm`, Slack asks first. Its dialog shows
the confirm text, the label, and **Back**. `tone`, `refresh`, and `link` have no
effect in Slack.

The `extra_actions` of a form are more buttons beside its action. Each one sends
the same fields.

Any other action of the context shows its [text form](#text-forms) in Slack: its
label, linked to the run's page. So does a form with `submit="change"`, because
it sends on each change. The person runs it there. An action in a cell of a
table shows its text form when it has fields, or when another control shares its
cell. So do the actions of a table that selects rows.

Slack shows each field as the input that a [form of a chat agent](chat.md#forms)
shows. A `TextField`, a `TextAreaField`, and a `NumberField` show as a text input.
A `SelectField` shows as a select. A `MultiSelectField` shows as a select of
several options. A `RadioField` shows as radio buttons. A `CheckboxField` shows
as a checkbox.

An input shows the label of its field, the placeholder, the help text, whether
the field is optional, and its value at the start. In Slack, a required `SelectField`
or `RadioField` whose value is not one of its options starts with no choice. The
person then picks an option. The dashboard starts it on its first option.

`review` refuses a `SecretField`, an `UploadField`, and a `MultiUploadField`
anywhere in its context, before the workflow waits for an answer. A chat message
holds no secret input and no file input. Ask for such a field on a page of the app
instead.

An action or a form shows all of its inputs and buttons, or none of them. It
shows its text form when an input or a button is past a limit of Slack. Two of
those limits are 2,000 characters for a label and 100 options for a select. It
also shows its text form when Slack has no input for one of its fields. The same
happens when the message has no room for all of them. A button then never sends
a form without one of its inputs.

The button names only the notification, the position of its block in the blocks,
and for a form, which of its actions the button runs. In a table, the button also
names the row and the cell. Druks reads the operation, the arguments, and the
fields from the blocks that it stored. A click then runs only an action that the
review offers.

Slack sends the answers of every input of the message with a click. Druks takes
only the answers of the inputs of the clicked block. The inputs of the approval
and of the other blocks stay out of the request. A field can then share its name
with a field of another block, or with the note of the approval. On a click,
Druks finds the account of the Slack user, as for an answer:

- A person whose Slack account is not connected gets a direct message with the
  link to connect it. The link returns to the run's page. On an installation
  with no endpoint, the message says where to connect the account. Nothing
  runs.
- In a chat, only the person of the chat runs its actions.
- An answered question and a past round run nothing.
- Each field checks its answer as a field of a chat agent's form does. A
  required field needs an answer. A number field takes a number within its
  minimum, its maximum, and its step. A choice takes one of its options. When a
  field refuses its answer, only the person who clicked reads why, with the label
  of the field. Nothing runs.

Druks answers Slack immediately. After that, it calls the route of the operation in
its own process, as the account. The request has the shape that the dashboard
sends: the arguments of the action first, then the answers. A number field sends
a number, or `null` when it is empty. See [Request shape](#request-shape).

The request names the account in a place that only Druks's own process can write. No
client can thus copy it. The route runs `current_account`, `require_app_access`,
and its own dependencies, as for the dashboard. A route that takes only the
identity of the signed-in person refuses the request.

When the route answers with any 2xx status, a line replaces the block of the
action. The line names the account and the label of the clicked action. It
replaces the block in the message and in the notification. A second click on an
action of the block then runs nothing. The line of a form replaces its inputs and
all of its buttons. In a table, the line replaces only the cell of the action, and
the other rows keep their buttons.

While the run waits, its page in Druks shows the action or the form too. That
page keeps no record of an action that ran from Slack. A recorded request on the
Activity page shows each button and input of its context disabled. The inputs and
the buttons of the approval stay. The workflow still waits for its answer.

When the route refuses, only the person who clicked reads why. A refused value
names its field. A 422 answer names each field and its error. The inputs and the
buttons stay. The person can then correct an answer and click again. A refusal
that gives no reason that Druks can read names its HTTP status.

One action of a message runs at a time. A click while one runs gets a reply that
says so.

Druks does not try a refused or failed action again. When Druks stops after the
route answers and before it records the line, the action can run once more.

## Status in Slack

A workflow shows where it stands with blocks. See
[Report status](writing-an-app.md#report-status). Slack draws the blocks of a
status as it draws the blocks of an approval, with no buttons. The link to the
run page follows them when the installation sets `urls.endpoint`. Each edit draws
them again.

While the workflow waits, and after it ends, Druks adds a `Callout` at the end of
the status. It says what the workflow waits for, or how it ended.

## Actions and links

An `Action` names an app-local operation:

```python
ui.Action(label="Archive", operation="archive_note", arguments={"note_id": note.id})
```

An action can collect values before it runs:

```python
ui.Action(
    label="Write a note",
    operation="write_note",
    tone="primary",
    fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
)
```

The shell shows the action as a control. When the action has fields, the shell
collects the values before it runs the operation.

When field entry is the main task of the page, use a `Form`. When field entry is
only a small part of the page, use an `Action` with fields.

The `operation` is the `operation_id` of one of the app's own routes:

```python
@router.post("/notes/{note_id}/archive", operation_id="archive_note")
async def archive_note(note_id: int) -> dict[str, str]: ...
```

The shell resolves the operation to its method and URL. The author writes no
URL.

Druks indexes every route that the app mounts by its `operation_id` at boot. Two
routes in one app with the same `operation_id` are a boot error. So is one
route that answers two methods under it. An action then names one operation and
calls one method.

An `Action` exists only after a page function runs. Druks checks the
reference when it builds the page, the earliest moment that the action exists. Druks
answers two failures with the page-read error. Each one names the operation:

- No route carries that `operation_id`.
- The route is a GET. A GET route is a read. It can never be an action.

The route takes its values the way the shell sends them. They are path
parameters and a flat JSON body, each value under its own name. Use
`Body(embed=True)` or a model, with no aliases and no query parameters. A route
of another shape answers 422 when the action runs.

An action target thus declares path parameters and a flat JSON body only:

```python
@router.post("/notes/{note_id}/archive", operation_id="archive_note")
async def archive_note(note_id: int, reason: Annotated[str, Body(embed=True)]):
    ...
```

The app roster at `GET /api/apps` carries the table that the renderer resolves
against. It lists the app's non-GET operations only:

```json
{
  "operations": [
    {"id": "archive_note", "method": "POST", "path": "/api/field_notes/notes/{note_id}/archive"}
  ]
}
```

App code never reads that table.

### Request shape

The shell builds one JSON object. It takes the action `arguments` first. Then
it adds the values from `Action.fields` or from the `Form` around the action. A field name
that is also an argument name is an error when Druks builds the page.

The shell fills the operation's path parameters from that object. It sends
every other key as the JSON request body. Authentication, authorization,
and request identity stay on the platform route.

### Results

| Field | Effect |
| --- | --- |
| `fields` | The shell collects these values before it runs the action. |
| `confirm` | Non-empty text. The shell asks before it sends. |
| `tone: "danger"` | The shell shows a destructive presentation. |
| `refresh: "page"` | The shell reads the whole page again. Default. |
| `refresh: "region"` | The shell reads the nearest named `Section` around the action. |
| `refresh: "none"` | The shell stays. |
| `link` | The shell navigates there. `refresh` does not apply. |

`refresh: "region"` needs an owner. The owner is the nearest named `Section`
that encloses the action. An action with no such ancestor and
`refresh: "region"` is a page-build error.

While the request runs, the shell shows a pending state and blocks a second
submission of the same action.

A 422 answer carries the platform validation envelope. The shell maps each
error whose `loc` ends with a field name to that field. It shows the others as
form errors.

A `Link` navigates and never calls an operation:

```python
ui.Link("History", page="peer_history", arguments={"peer_id": peer.id})
ui.Link("Provider status", url="https://status.example.com")
```

A `Link` sets exactly one of `page`, `url`, and `subject`. Druks rejects a
`Link` that sets none or more than one when it builds the page. An argument can
be a number. The link carries it as text.

A link on a value shows the value's words. It needs no label:

```python
ui.TextValue(peer.name, link=ui.Link(page="peer", arguments={"peer_id": peer.id}))
```

A link that stands on its own needs a label. Druks rejects one without a label
when it builds the page.

A `page` names a declared page of the same app. `arguments` fills that page's
route parameters. The shell resolves both against the page table. It shows a
`Link` that it cannot resolve as broken, and names the page that it wanted. The
rest of the page still renders.

An absolute `url` opens in a new tab. A relative `url` stays in this tab. A
path under `/api/`, such as `/api/oauth/acme/connect`, loads from the server.
Every other path opens in the shell.

`Link` and `Action` are different public types. Both are blocks. A page can
hold one directly. Every container's `controls` holds either one.

## How to read the model listings

Each listing that follows gives every field that a model carries and the JSON
that it sends.
Four rules apply to every one of them.

**A listing is the whole shape, not the class body.** Some models share a base.
Every field carries a name and a label. A section, a card and a column all
carry blocks. A listing shows what the model carries, whichever class declares
it. A sample fixes the keys and their values, never their order.

**A discriminator carries its own literal as its default.** `Text.block` is
`Literal["text"] = "text"`. The author writes `Text("…")` and never passes
the discriminator.

**Wire names are camelCase.** `alternative_text` serializes as
`alternativeText`. `Schema` does that for every model here.

**Druks coerces author input to the wire type.** These fields take a friendlier
input than they store:

| Field | Author passes | Druks stores |
| --- | --- | --- |
| `Page.follows`, `Section.follows`, `Link.subject`, `GateControls.subject`, `SubjectStatus.subject` | a subject | `Follows` |
| `Files.files` | `druks.files.File` objects | `list[FileSummary]` |
| `Metric.value`, `Fact.value`, `TableRow.cells`, `List.items` | a `str` | `TextValue` |
| `Metric.value`, `Fact.value`, `TableRow.cells`, `List.items` | an `int` or a `float` | `NumberValue` |
| `Metric.value`, `Fact.value`, `TableRow.cells`, `List.items` | a `Value` | the same value |

A `str`, an `int`, or a `float` is the short form of a value. The short form
gets the defaults of its `Value`. A `TextValue` then has no description and no
link. A `NumberValue` has no unit and a neutral tone.

For anything more, give the `Value` itself. A number must be finite. Druks
refuses a `bool`, because Python counts it as a number and a reader does not.

## The three unions

```python
Block = Annotated[
    Text | Markdown | Quote | Section | Card | Cards | Callout | Divider
    | EmptyState
    | Link | Action | Form | Timeline | Progress | Image | Files
    | GateControls | Chart | ImageGallery | Metrics | Facts | Table | List
    | Stack | Columns,
    Discriminator("block"),
]

Value = Annotated[
    TextValue | NumberValue | StatusValue | SubjectStatus | TimeValue | ControlsValue,
    Discriminator("value"),
]

Field = Annotated[
    TextField | TextAreaField | NumberField | SelectField | MultiSelectField
    | RadioField | CheckboxField | UploadField | MultiUploadField | SecretField,
    Discriminator("field"),
]
```

A payload whose discriminator is not in its union fails validation. The shell
shows an app-scoped error and names the block.

## Blocks

Every block carries a `block` discriminator.

A page fills the screen that the shell gives it. Each kind of block uses the
room in its own way. A table, an image gallery, `Columns`, a timeline and a metric row
take the width. A fact list is as wide as its facts.

A chart and a progress bar grow with the page and stop where more width does
not help them. Prose keeps a line length, measured against the type size rather
than the screen. It stays readable at every width of the display.

Some blocks have one required value that is the thing they show: their words,
their content, or their identity. Such a block takes that value positionally.
It takes every other value by keyword:

```python
ui.Text("Three peers answered in the last hour.")
ui.Callout("Notes arrive through the API.", tone="info", title="Not here yet")
ui.Link("Open", page="note", arguments={"note_id": "7"})
```

A container that holds one list takes that list positionally too:

```python
ui.Metrics([ui.Metric("Captured", 12)], title="Counts")
ui.Stack([ui.Text("one"), ui.Divider()], gap="large")
```

`Metrics`, `Facts`, `List`, `Timeline`, `Files`, `ImageGallery`, `Stack`,
`Columns`, and `TableRow` all take that list positionally. A label-and-value pair, `Metric`
or `Fact`, takes both positionally.

A block that holds more than one thing names every argument: `Card`, `Section`,
`Table`, `Chart`, and `Cards`. `Cards` is on that list because its `empty` is
content, not decoration. So is `Form`. Its required value is the action that
sends it, not something it shows. The author always writes `action=`.

### Text

```python
class Text:
    block: Literal["text"] = "text"
    text: str
```

```json
{"block": "text", "text": "Three peers answered in the last hour."}
```

### Markdown

```python
class Markdown:
    block: Literal["markdown"] = "markdown"
    text: str
```

```json
{"block": "markdown", "text": "## Report\n\nThe sweep found **2** stale peers."}
```

The shell renders the markdown. It strips raw HTML.

### Quote

```python
class Quote:
    block: Literal["quote"] = "quote"
    text: str
```

```json
{"block": "quote", "text": "No access to that repository.\nFalling back."}
```

A quote holds someone else's words as they arrived: a message, a reply, an
answer. Line breaks survive. The shell reads nothing as markup. `Text` joins the
lines into one paragraph. A `Markdown` block rewrites the text.

### Section

```python
class Section:
    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    controls: list[Action | Link] = []
    blocks: list[Block] = []
    follows: Follows | None = None
```

```json
{
  "block": "section",
  "title": "Decision",
  "name": "decision",
  "controls": [],
  "blocks": [],
  "follows": {"subjectType": "peers", "subjectId": "42"}
}
```

A section's controls belong to that section. The shell chooses where to show
them. An action in `blocks` stays with the body content.

The controls are inside the section region. A region refresh then replaces the
heading, the controls, and the body together.

### Card

```python
class Card:
    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    blocks: list[Block] = []
    controls: list[Action | Link] = []
    link: Link | None = None
    drag: dict = {}
```

```json
{
  "block": "card",
  "title": "peer-7",
  "description": "Last answered 4 minutes ago.",
  "blocks": [{"block": "text", "text": "Healthy."}],
  "controls": [],
  "link": {"block": "link", "label": "peer-7", "page": "peer", "arguments": {"peer_id": "7"}, "url": "", "subject": null},
  "drag": {}
}
```

`link` is the card's destination. With no `controls`, the shell makes the whole
panel the control. With `controls`, the title carries the link. A button is then
never inside an anchor. A linked card must not hold other links in
`blocks`.

`drag` is what a [`Cards.drop`](#cards) action receives. An empty `drag` means the card
does not move.

### Cards

```python
class Cards:
    block: Literal["cards"] = "cards"
    title: str = ""
    cards: list[Card] = []
    empty: EmptyState | None = None
    layout: Literal["wrap", "stack", "tiles"] = "wrap"
    drop: Action | None = None
```

```json
{
  "block": "cards",
  "title": "Peers",
  "cards": [{"block": "card", "title": "peer-7", "description": "", "blocks": [], "controls": [], "link": null, "drag": {}}],
  "empty": null,
  "layout": "wrap",
  "drop": null
}
```

A `Cards` block shows one card for each of a set of things.

```python
ui.Cards(
    title="Peers",
    cards=[
        ui.Card(
            title=peer.name,
            blocks=[...],
            link=ui.Link(peer.name, page="peer", arguments={"peer_id": peer.id}),
        )
        for peer in peers
    ],
    empty=ui.EmptyState("No peer yet", controls=[ui.Link("Add one", page="new_peer")]),
)
```

`wrap` (the default) puts as many cards in a row as the screen holds. `stack`
is one column, for a board of statuses. `tiles` is a wrap of squares. An image
fills each card. The shell crops the image to cover the card rather than show
it in full.

`drop` is the action that a card submits when a person drags it onto this list.
The shell merges the card's `drag` into the action arguments and runs the
operation. The drop is the submit. `drop` cannot set `fields` or `confirm`.

A drop onto the same list does nothing. A click on a linked card still opens it.
While the operator drags a card, the shell dims it and shows a placeholder in the
list under the pointer. The action runs only on drop.

With no cards, the shell shows the title and `empty` in their place. With no
cards and no `empty`, it shows nothing unless the list sets `drop`. An empty
column can then still receive a card. `Table` does the same for `empty`.

`empty` takes an `EmptyState`, not a line of text, because an empty page
usually must say what to do next.

### Callout

```python
class Callout:
    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str
    controls: list[Action | Link] = []
```

```json
{"block": "callout", "tone": "warning", "title": "Stale", "text": "No answer for 2 days.", "controls": []}
```

`controls` are the next step that the message points at. The shell shows them
under the text.

### Divider

```python
class Divider:
    block: Literal["divider"] = "divider"
```

```json
{"block": "divider"}
```

### EmptyState

```python
class EmptyState:
    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    controls: list[Action | Link] = []
```

```json
{
  "block": "empty_state",
  "title": "No peers yet",
  "description": "Add the first peer to start a sweep.",
  "controls": []
}
```

### Link

```python
class Link:
    block: Literal["link"] = "link"
    label: str = ""
    page: str = ""
    arguments: dict[str, str] = {}
    url: str = ""
    subject: Follows | None = None
```

```json
{"block": "link", "label": "History", "page": "peer_history", "arguments": {"peer_id": "7"}, "url": "", "subject": null}
```

A link sets exactly one destination. `page` is another page of this app. `url`
is an outside address. `subject` is the app's page for that subject. That page
shows everything that Druks did about the subject. No app page
builds that again:

```python
ui.Link("Everything druks did", subject=found)
```

The shell adds that link to every page that follows one subject.

A service that an app declares with `with_scopes()` has the address where an
operator connects it. The provider then returns the operator to the app:

```python
ui.Link("Add an account", url=FieldNotes.calendar.connect_url)
```

### Action

```python
class Action:
    block: Literal["action"] = "action"
    label: str
    operation: str
    arguments: dict = {}
    fields: list[Field] = []
    tone: Literal["default", "primary", "danger"] = "default"
    confirm: str = ""
    refresh: Literal["none", "page", "region"] = "page"
    link: Link | None = None
    url: str = ""
```

```json
{
  "block": "action",
  "label": "Archive",
  "operation": "archive_note",
  "arguments": {"note_id": 7},
  "fields": [],
  "tone": "danger",
  "confirm": "Archive this note?",
  "refresh": "page",
  "link": null
}
```

`url` is the page where a person runs the action. Druks sets it when `review`
stores the context. See [Text forms](#text-forms). The JSON does not include an
empty `url`.

`arguments` keys are the operation's own parameter names. Druks serializes
them unchanged. A route parameter keeps its Python spelling on the wire.

An action with fields stays an action in the declaration and on the wire. The
shell decides how to collect the fields.

An action cannot set both `fields` and `confirm`. When a page function builds
this action, Druks refuses it. Each option asks the operator before the action
runs.

After a send, the shell navigates when the action sets `link`, or when the
operation answers exactly `{"url": "https://..."}`. The URL must be absolute
`http` or `https`. An answer with any other field opens no page. `refresh`
applies only when neither navigates.

### Form

```python
class Form:
    block: Literal["form"] = "form"
    title: str = ""
    description: str = ""
    fields: list[Field] = []
    action: Action
    extra_actions: list[Action] = []
    submit: Literal["button", "change"] = "button"
    layout: Literal["stack", "prose", "row"] = "stack"
```

```python
ui.Form(
    title="Write a note",
    fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
    action=ui.Action(label="Save", operation="write_note", tone="primary"),
)
```

When the page exists to collect the values, use a `Form`. When the page has
another main task, use an `Action` with `fields`.

A form keeps all its fields on the form. When a form action also has fields,
Druks refuses the form when the page function builds it.

`extra_actions` are more buttons that send the same fields.

```json
{
  "block": "form",
  "title": "Write a note",
  "description": "",
  "fields": [
    {
      "field": "text_area",
      "name": "body",
      "label": "Note",
      "value": "",
      "placeholder": "",
      "helpText": "",
      "isRequired": true,
      "rows": 4,
      "isMarkdown": false
    }
  ],
  "action": {
    "block": "action",
    "label": "Save",
    "operation": "write_note",
    "arguments": {},
    "fields": [],
    "tone": "primary",
    "confirm": "",
    "refresh": "page",
    "link": null
  },
  "extraActions": [],
  "submit": "button",
  "layout": "stack"
}
```

`submit="change"` sends the form when a select changes or a text field blurs.
The shell draws no button. `layout="prose"` is a title and a body. `layout="row"`
is a labeled property. A form cannot both submit on change and set `confirm`
or `extra_actions`.

### Timeline

```python
class TimelineItem:
    when: AwareDatetime
    title: str
    description: str = ""
    status: StatusValue | None = None


class Timeline:
    block: Literal["timeline"] = "timeline"
    title: str = ""
    items: list[TimelineItem] = []
```

```json
{
  "block": "timeline",
  "title": "Sweep",
  "items": [
    {
      "when": "2026-08-29T09:14:02Z",
      "title": "Run started",
      "description": "",
      "status": {"value": "status", "label": "active", "tone": "active", "link": null}
    }
  ]
}
```

`when` must name an offset. Druks can then order items from different
sources. Druks orders the items oldest first. It compares the stamps at their
full precision. Items that share a moment keep their declared order. A snapshot
arrives in the order that the shell shows it.

### Progress

```python
class ProgressStep:
    label: str
    status: StatusValue


class Progress:
    block: Literal["progress"] = "progress"
    label: str
    completed: float | None = None
    total: float = 1.0
    steps: list[ProgressStep] = []
```

`completed` is a meaningful optional value. It runs from 0 to `total`. `total`
is more than zero. `completed` carries three shapes:

| Shape | Declaration |
| --- | --- |
| determinate | `completed` set, `steps` empty |
| indeterminate | `completed` unset, `steps` empty |
| staged | `steps` set |

A `Progress` that gives both `completed` and `steps` is a validation error. So is
a value outside those ranges, or a value that is not a number.

```json
{
  "block": "progress",
  "label": "Sweeping peers",
  "completed": 3.0,
  "total": 8.0,
  "steps": []
}
```

The shell renders a determinate or indeterminate shape as text and as an ARIA
progress bar. A screen reader then gets the same state as the eye. Staged work
has no measurable value. The shell renders it as a named group in which each
step announces its own state.

### Image

```python
class Image:
    block: Literal["image"] = "image"
    url: str
    alternative_text: str
    caption: str = ""
```

```json
{
  "block": "image",
  "url": "/api/files/018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77",
  "alternativeText": "Latency over the last day, flat at 40 ms.",
  "caption": "Peer latency"
}
```

An image needs `alternative_text`. Text that is only whitespace is a validation
error. When the image does not load, the shell shows the alternative text in
its place.

### Files

```python
class FileSummary:
    id: str
    name: str
    content_type: str
    size: int
    url: str


class Files:
    block: Literal["files"] = "files"
    title: str = ""
    files: list[FileSummary] = []
```

`files` accepts `druks.files.File` objects. Druks reads the name, media type,
and size from the file record. It derives `url` from the id. A download of the file then
always uses the platform route.

```json
{
  "block": "files",
  "title": "Report",
  "files": [
    {
      "id": "018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77",
      "name": "sweep.csv",
      "contentType": "text/csv",
      "size": 4211,
      "url": "/api/files/018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77"
    }
  ]
}
```

The shell previews an image. Every file gets a download through
`/api/files/{id}`, which keeps the platform's own authentication.

### GateControls

```python
class GateControls:
    block: Literal["gate_controls"] = "gate_controls"
    subject: Follows
```

```json
{"block": "gate_controls", "subject": {"subjectType": "peer", "subjectId": "7"}, "status": {"state": "parked", "run": "run-6f0a", "kind": "reports.audit", "agent": null, "gate": "review", "failure": null, "reason": null, "triggeredAt": "2026-08-29T09:14:02Z", "accountUsername": "operator", "phase": null, "phaseEndsAt": null}}
```

The author writes `ui.GateControls(peer)`. Druks adds `status`, the status that
the board reads, when it serves the page.

The name is `GateControls`. `druks.ui` has no type named `Gate`. `Gate` is the
workflow-side declaration in `druks.workflows`.

### Chart

```python
class ChartSeries:
    label: str
    points: list[float]


class Chart:
    block: Literal["chart"] = "chart"
    kind: Literal["line", "bar", "area", "pie"] = "line"
    title: str = ""
    categories: list[str] = []
    series: list[ChartSeries] = []
    category_label: str = ""
    value_label: str = ""
```

```json
{
  "block": "chart",
  "kind": "bar",
  "title": "Answers per day",
  "categories": ["Mon", "Tue", "Wed"],
  "series": [{"label": "peer-7", "points": [3.0, 5.0, 4.0]}],
  "categoryLabel": "Day",
  "valueLabel": "Answers"
}
```

Every series must have one point for each category. Every point must be a
number that JSON can carry. The shell renders a table of the same numbers, titled
with the chart's own title and value label, for a screen reader.

A line, bar, or area chart draws its categories along an axis. A pie draws no
axes. It draws a slice for each category. The slice is the share of the
category in the whole:

```python
ui.Chart(
    kind="pie",
    title="Answers by source",
    categories=["Web", "API", "Mail"],
    series=[ui.ChartSeries(label="Answers", points=[12, 5, 3])],
)
```

A pie has one series. Each of its points is more than zero. Druks refuses another
shape when it builds the block. The error names the chart by its title,
or by its categories when it has no title.

The dashboard colors the slices with the four chart series colors in turn,
clockwise from the top. A line in the color of the surface separates two slices.
The legend shows the color, the category, and the value of each slice.

### ImageGallery

```python
class ImageGallery:
    block: Literal["image_gallery"] = "image_gallery"
    title: str = ""
    images: list[Image] = []
```

```json
{
  "block": "image_gallery",
  "title": "Screenshots",
  "images": [
    {"block": "image", "url": "/api/files/a", "alternativeText": "Login page.", "caption": ""}
  ]
}
```

### Metrics

```python
class Metric:
    label: str
    value: Value
    description: str = ""


class Metrics:
    block: Literal["metrics"] = "metrics"
    title: str = ""
    metrics: list[Metric] = []
```

```json
{
  "block": "metrics",
  "title": "",
  "metrics": [
    {
      "label": "Open peers",
      "value": {"value": "number", "number": 12.0, "unit": "", "tone": "neutral"},
      "description": "Peers with an unanswered sweep."
    }
  ]
}
```

### Facts

```python
class Fact:
    label: str
    value: Value


class Facts:
    block: Literal["facts"] = "facts"
    title: str = ""
    facts: list[Fact] = []
```

```json
{
  "block": "facts",
  "title": "Peer",
  "facts": [
    {"label": "Name", "value": {"value": "text", "text": "peer-7", "description": "", "link": null}},
    {"label": "State", "value": {"value": "status", "label": "parked", "tone": "warning", "link": null}}
  ]
}
```

`Facts` is the label-and-value list. The contract has no type named
`KeyValue`.

### Table

```python
class TableColumn:
    label: str
    align: Literal["start", "end"] = "start"


class TableRow:
    cells: list[Value] = []
    detail: str = ""
    key: str = ""


class Table:
    block: Literal["table"] = "table"
    title: str = ""
    columns: list[TableColumn] = []
    rows: list[TableRow] = []
    empty: EmptyState | None = None
    select: str = ""
    actions: list[Action] = []
```

```json
{
  "block": "table",
  "title": "Peers",
  "columns": [{"label": "Peer", "align": "start"}, {"label": "Answers", "align": "end"}],
  "rows": [
    {
      "cells": [
        {"value": "text", "text": "peer-7", "description": "", "link": {"block": "link", "label": "peer-7", "page": "peer", "arguments": {"peer_id": "7"}, "url": "", "subject": null}},
        {"value": "number", "number": 12.0, "unit": "", "tone": "neutral"}
      ],
      "detail": "",
      "key": ""
    }
  ],
  "empty": {"block": "empty_state", "title": "No peers yet.", "description": "", "controls": []},
  "select": "",
  "actions": []
}
```

Every row must have one cell for each column. With no rows the shell shows
`empty`, and nothing of its own. A wide table scrolls inside its own
container, on a narrow screen as well. A stacked row loses the header that each
cell belongs to.

A row's `detail` is the sentence that it has no room for, such as the cause
of a status or the reason for a verdict. The shell keeps it folded until the
reader opens it. Twenty rows that stopped for one reason then do not cost twenty
page loads. It is text, not blocks.

`select` names the argument that takes the selected row keys, as a list of
strings. `actions` run on that list. They cannot collect fields. Set both or
neither. On a table with `select`, each row needs its own `key`.

### List

```python
class List:
    block: Literal["list"] = "list"
    title: str = ""
    items: list[Value] = []
```

```json
{
  "block": "list",
  "title": "Recent notes",
  "items": [{"value": "text", "text": "Fan noise on rack 3.", "description": "", "link": null}]
}
```

### Stack

```python
class Stack:
    block: Literal["stack"] = "stack"
    gap: Literal["small", "medium", "large"] = "medium"
    blocks: list[Block] = []
```

```json
{"block": "stack", "gap": "medium", "blocks": []}
```

A `Stack` that holds no block is false. `if stack:` then asks whether it shows
something. Every other block is always true.

### Columns

```python
class Columns:
    block: Literal["columns"] = "columns"
    layout: Literal["even", "sidebar", "split"] = "even"
    blocks: list[Block] = []
```

```json
{"block": "columns", "layout": "even", "blocks": []}
```

Each child block is one column. `even` shares the width. `sidebar` keeps the
last column narrow. On a narrow screen, the columns stack.

`split` is the page. It is the only block: exactly two panes that scroll on
their own. The first pane is a list. The second is what that list opened.

`Stack` and `Columns` hold every V1 block, including each other. `split` is
the exception. It is the page, not a region inside one.

`Columns` is geometry. Each child is one column, for any number of children. For a
collection of cards, use `Cards`. The shell chooses how many cards a row holds.

## Values

Every value carries a `value` discriminator. A value renders the same way in
`Facts`, `Metrics`, `List`, and `Table`.

### TextValue

```python
class TextValue:
    value: Literal["text"] = "text"
    text: str
    description: str = ""
    link: Link | None = None
```

```json
{"value": "text", "text": "peer-7", "description": "the fastest peer", "link": null}
```

`link` is how a table cell, a fact, or a list item opens another page.
`description` says what the thing is, on a quieter second line under its name.
A name then needs no column of its own to explain it.

### NumberValue

```python
class NumberValue:
    value: Literal["number"] = "number"
    number: float
    unit: str = ""
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
```

```json
{"value": "number", "number": 40.0, "unit": "ms", "tone": "neutral"}
```

A `tone` colors a count that is itself the warning, such as how many need
attention. A figure that is only a figure stays `neutral`.

### StatusValue

```python
class StatusValue:
    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
    link: Link | None = None
```

```json
{"value": "status", "label": "parked", "tone": "warning", "link": null}
```

The app writes the word. The tone selects the presentation. The contract has
no type named `Status`. `active` means work that is in progress. A settled fact
uses another tone. `link` opens the thing that it names.

### SubjectStatus

```python
class SubjectStatus:
    value: Literal["subject_status"] = "subject_status"
    subject: Follows
    working: str = "working"
    status: dict | None = None
```

```json
{"value": "subject_status", "subject": {"subjectType": "target", "subjectId": "7"}, "working": "auditing", "status": {"state": "failed", "run": "run-6f0a", "kind": "reports.audit", "agent": null, "gate": null, "failure": "The crawl timed out.", "reason": null, "triggeredAt": "2026-08-29T09:14:02Z", "accountUsername": "operator", "phase": null, "phaseEndsAt": null}}
```

The author writes `ui.SubjectStatus(target, working="auditing")`. Druks adds
`status`, the status that the board reads, when it serves the page or reads the
ask of a review.

### TimeValue

```python
class TimeValue:
    value: Literal["time"] = "time"
    when: AwareDatetime | None
    empty: str = ""
```

```json
{"value": "time", "when": "2026-08-29T09:14:02Z", "empty": ""}
```

`when` must name an offset. The shell shows a relative time, and the exact
time in the title attribute. With no `when`, it shows `empty`:

```python
ui.TimeValue(target.last_audit_at, empty="never")
```

### ControlsValue

```python
class ControlsValue:
    value: Literal["controls"] = "controls"
    controls: list[Action | Link] = []
```

```json
{
  "value": "controls",
  "controls": [
    {
      "block": "action",
      "label": "Post",
      "operation": "write_note",
      "arguments": {},
      "fields": [],
      "tone": "primary",
      "confirm": "",
      "refresh": "page",
      "link": null
    }
  ]
}
```

A `ControlsValue` puts actions and links in a cell, a fact, or a list item.
Druks checks them like any other control. `refresh="region"` needs a named
Section. An unknown operation makes the page fail.

## Fields

Every field carries a `field` discriminator, `name`, `label`, `help_text`, and
`is_required`. `name` is the key that the shell sends. `UploadField`,
`MultiUploadField`, and `SecretField` carry no `value`.

| File field | Selection | Submitted value |
| --- | --- | --- |
| `UploadField` | One file | One file id, or `null` when empty |
| `MultiUploadField` | Several files in one dialog | A list of file ids, or `[]` when empty |

### TextField

```python
class TextField:
    field: Literal["text"] = "text"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "text", "name": "title", "label": "Title", "value": "", "placeholder": "", "helpText": "", "isRequired": true}
```

### TextAreaField

```python
class TextAreaField:
    field: Literal["text_area"] = "text_area"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
    rows: int = 4
    is_markdown: bool = False
```

```json
{"field": "text_area", "name": "body", "label": "Note", "value": "", "placeholder": "", "helpText": "", "isRequired": false, "rows": 4, "isMarkdown": false}
```

`is_markdown=True` keeps the value as markdown source. The shell renders
formatted text in place. A selection toolbar applies marks, headings, lists, and
links. Submit sends markdown. Read-only `Markdown` blocks use the GFM renderer.

### NumberField

```python
class NumberField:
    field: Literal["number"] = "number"
    name: str
    label: str
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "number", "name": "budget", "label": "Budget", "value": null, "minimum": 0.0, "maximum": 100.0, "step": 1.0, "helpText": "", "isRequired": false}
```

### SelectField

```python
class Option:
    value: str
    label: str
    group: str = ""


class SelectField:
    field: Literal["select"] = "select"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False
```

```python
ui.SelectField(
    name="repo_id",
    label="Repo",
    options=[
        ui.Option("acme/app", value="12", group="Acme"),
        ui.Option("acme/docs", value="13", group="Acme"),
        ui.Option("beta/api", value="14", group="Beta"),
    ],
    value="12",
    is_required=True,
)
```

```json
{
  "field": "select",
  "name": "severity",
  "label": "Severity",
  "options": [{"value": "low", "label": "Low", "group": ""}, {"value": "high", "label": "High", "group": ""}],
  "value": "low",
  "helpText": "",
  "isRequired": true
}
```

A non-empty `group` nests the option in an `<optgroup>` of that name.
Consecutive options that share a group share one group. An empty `group` is a
flat choice. A `RadioField` and a `MultiSelectField` ignore `group`.

A required select whose `value` is not among the options starts on the first
option. Otherwise a submit of the empty declared value sends a blank to the
operation. An optional select
whose `value` is not among the options shows a blank choice, `—`, until the
operator picks an option.

### MultiSelectField

```python
class MultiSelectField:
    field: Literal["multi_select"] = "multi_select"
    name: str
    label: str
    options: list[Option] = []
    value: list[str] = []
    help_text: str = ""
    is_required: bool = False
    is_searchable: bool = False
```

```json
{
  "field": "multi_select",
  "name": "tags",
  "label": "Tags",
  "options": [{"value": "rack", "label": "Rack", "group": ""}],
  "value": ["rack"],
  "helpText": "",
  "isRequired": false,
  "isSearchable": false
}
```

`is_searchable` puts a search box above a long list of options. The shell
filters the options in the browser by their label. Upper and lower case match.
The shell does not reread the page. A hidden option keeps its check. The form
sends every checked option, shown or not.

### RadioField

```python
class RadioField:
    field: Literal["radio"] = "radio"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{
  "field": "radio",
  "name": "decision",
  "label": "Decision",
  "options": [{"value": "approve", "label": "Approve", "group": ""}],
  "value": "",
  "helpText": "",
  "isRequired": true
}
```

### CheckboxField

```python
class CheckboxField:
    field: Literal["checkbox"] = "checkbox"
    name: str
    label: str
    value: bool = False
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "checkbox", "name": "notify", "label": "Notify the owner", "value": false, "helpText": "", "isRequired": false}
```

### UploadField

```python
class UploadField:
    field: Literal["upload"] = "upload"
    name: str
    label: str
    accept: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "upload", "name": "photo", "label": "Add a photo", "accept": "image/*", "helpText": "", "isRequired": false}
```

The field takes one file and starts empty. Nothing that the server
sends can fill a file input.

Druks passes `accept` as it is to the filter of the file picker, in its own syntax,
such as `"image/*"` or `".csv,.tsv"`. It narrows what the operator can pick. It
is not a promise about the bytes. The platform does not check it. An operation
that needs certainty opens the file and examines it.

On submit the shell sends the bytes to `POST /api/<app>/uploads`, which stores
them and answers with a `FileSummary`. The shell then submits `id` as the
field's value. The operation takes a plain string:

```python
@router.post("/photos", operation_id="add_photo")
async def add_photo(photo: Annotated[str, Body(embed=True)]) -> None:
    shop.image = File(id=photo)
```

A `FileField` column takes a `File` and stores its id as a foreign key. The
database refuses an id that names no file.

Druks stores the upload under the app whose page holds the form and under the
operator who sent it. It takes both from the request, not from the client.
Druks refuses a file over the platform's upload cap. The shell puts the refusal
on that field. A file whose form the operator never submits stays stored, and
nothing points to it.

### MultiUploadField

```python
class MultiUploadField:
    field: Literal["multi_upload"] = "multi_upload"
    name: str
    label: str
    accept: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "multi_upload", "name": "photos", "label": "Photos", "accept": "image/*", "helpText": "", "isRequired": false}
```

`MultiUploadField` lets the operator select several files in one native file
dialog. The shell shows their names and a Remove button for each file before
submit. Another selection replaces the list. The field has no `value`. A page
cannot put stored files into the picker.

`accept` has the same file-dialog filter syntax as
[`UploadField`](#uploadfield). It does not validate the bytes. `is_required=True`
requires at least one selected file. An empty optional field submits `[]`.

You can put the field in `Action.fields` and in `Form.fields`. For example, this
action collects photos before it calls the operation:

```python
ui.Action(
    label="Add photos",
    operation="add_photos",
    fields=[ui.MultiUploadField(name="photos", label="Photos", accept="image/*")],
)
```

On submit, the shell uploads each file through `POST /api/<app>/uploads`.
The `MAX_UPLOAD_BYTES` limit applies to each file. After all uploads
succeed, the shell sends `{"photos": ["file-id-1", "file-id-2"]}` to the
operation. Its parameter is `photos: Annotated[list[str], Body(embed=True)]`.

If an upload fails, the shell marks the field in error and does not call the
operation. It keeps the selected files for correction or another attempt.
Files from successful uploads remain stored even if a later upload fails.
After a successful submit, the shell clears the selection.

### SecretField

```python
class SecretField:
    field: Literal["secret"] = "secret"
    name: str
    label: str
    help_text: str = ""
    is_required: bool = False
```

```python
ui.SecretField(name="token", label="Access token", help_text="From your account settings.")
```

```json
{"field": "secret", "name": "token", "label": "Access token", "helpText": "From your account settings.", "isRequired": false}
```

A `SecretField` takes one secret that the operator gives: a token, a key.

It has no `value`. No page can fill a file input. A page must not fill a secret. A
field with nowhere to put one cannot return a stored secret to the browser.

The shell masks it. The password managers of the browser do not get it. A
successful submit leaves it empty.

Masking protects the screen. It does not protect the stored secret.

A refusal never shows the server's words on a secret field. The shell shows a
fixed line, because a validation message can quote the submitted value. It
shows a fixed line on the form too, for a refusal that names no field on screen.
Every other field keeps the server's own words.

## Page and Follows

```python
class Follows:
    subject_type: str
    subject_id: str = ""


class Page:
    title: str
    description: str = ""
    controls: list[Action | Link] = []
    filters: list[Field] = []
    blocks: list[Block] = []
    follows: Follows | None = None
```

```json
{
  "title": "peer-7",
  "description": "One peer and its last sweep.",
  "controls": [],
  "filters": [],
  "blocks": [{"block": "text", "text": "Healthy."}],
  "follows": {"subjectType": "peers", "subjectId": "7"}
}
```

A page's controls belong to that page. The shell chooses where to show them. An
action in `blocks` stays with the body content.

`filters` are fields that the shell renders in the page chrome. A change to one
updates the URL query and rereads the page. An empty `value` means any. They are not
actions. They do not call an operation.

`Page`, `Section`, `Card`, `Callout`, and `EmptyState` all take `controls` the
same way. `controls` is a list of `Action` and `Link`, in the order that the app
wants them read.

An `Action` calls one of the app's operations. A `Link` navigates. Both are
things that an operator presses. They share the row. A `Card` can also take
`link`. That is the card's destination, not a control on the row.

```python
return ui.Page(
    "Peers",
    controls=[
        ui.Action(
            label="Add a peer",
            operation="add_peer",
            fields=[ui.TextField(name="repo", label="Repository", is_required=True)],
        ),
        ui.Link("What a peer is", url="https://docs.druks.ai/peers"),
    ],
    blocks=[ui.Table(...)],
)
```

## Errors

The shell keeps a failure inside the app surface. It never makes the
dashboard fail.

| Failure | Answer |
| --- | --- |
| A page function raises | The page API answers 500 and `PAGE_FAILED`. The answer names the app and the page. What the app's own code said stays in the process log. It can carry a query, a URL, or a credential. The shell shows an app-scoped error and a retry control. |
| A page answers with something that is not a `Page` | The same answer. It says what the page answered with. |
| A payload fails validation | The shell shows an app-scoped error. It renders the rest of the dashboard. |
| An unknown discriminator | The shell shows an app-scoped error and names the block. |
| A stream disconnects | The shell reconnects. The last good snapshot stays on screen. |

## Demand-pulled

These have agreed names. V1 does not have them. Druks adds each one when an
app needs it:

- `MoneyValue`
- `PercentValue`
- `DurationValue`
- Date and time input fields.

## Not in V1

V1 has no `Tabs` block, no accordion, no inline reveal form, and no general
client-state API. A table row folds its `detail`. An action can collect
fields before it runs. In both cases, the shell owns the interface.

Static child pages already give tabs. The URL holds the current one. An app
that needs a control that the contract does not have ships an ESM frontend.
