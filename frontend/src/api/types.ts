/** Shared API response shapes. App-specific types stay with their app. */


// durable/schemas.py serves these shapes for every app. An app keys its board and
// its detail page on its own subject summary. SubjectRow and SubjectResponse take
// that summary as a type parameter.

// The lifecycle states of a run.
export type RunState =
  | 'scheduled'
  | 'running'
  | 'parked'
  | 'finished'
  | 'failed'
  | 'cancelled'
  // The DBOS workflow row of the run is gone. The run never starts.
  | 'orphaned'

export interface DashboardRun {
  app: string
  run: string
  kind: string
  state: RunState
  subjectType: string | null
  subjectId: string | null
  subjectKey: string | null
  updatedAt: string
  parkedAt: string | null
  requestLabel: string | null
  artifactTitle: string | null
  presentation: string | null
  requestUrl: string | null
  failure: string | null
}

export interface DashboardSection {
  total: number
  rows: DashboardRun[]
}

export interface DashboardOverview {
  needsYou: DashboardSection
  running: DashboardSection
  failed: DashboardSection
  lastFinishedAt: string | null
  lastFailedAt: string | null
}

export interface ScheduledRun {
  run: string
  status: string
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
}

export interface DashboardSchedule {
  app: string
  kind: string
  cron: string | null
  defaultCron: string
  enabled: boolean
  timezone: string
  nextRunAt: string | null
  runs: ScheduledRun[]
}

export interface DashboardSchedules {
  rows: DashboardSchedule[]
}

// The header that the platform shows above a subject. ``id`` keys its status, its
// timeline, and its detail URL. ``key`` is its name.
export interface SubjectSummary {
  id: string
  key: string
}

export interface SubjectStatus {
  // Null when no run drives the subject. Druks has not run it.
  state: RunState | null
  // The driving run's id.
  run: string | null
  // The app writes the words of its board lane from these facts. The backend sends
  // no prose. ``kind`` is the kind of the driving run. While the run runs, ``agent``
  // is the agent of its latest agent call.
  kind: string | null
  agent: string | null
  // The gate of a parked run. The app maps it to its own words.
  gate: string | null
  // ``failure`` tells why the failed driving run stopped. ``reason`` is the class of
  // the stop for code. "gate_timeout" is an unanswered gate, not a crash.
  failure: string | null
  reason: string | null
  // When Druks last took up this subject, and whose work it is. A subject with a row
  // of its own would keep these facts in its columns.
  triggeredAt: string | null
  accountUsername: string | null
}

export interface TokenUsage {
  inputTokens: number
  outputTokens: number
  cachedInputTokens: number
  cacheCreationTokens: number
  reasoningTokens: number
  totalTokens: number
}

export interface AgentCallSummary {
  id: string
  // The agent that made this call, such as "summarize". ``label`` is its display name.
  agent: string
  label: string
  /** The subscription owner. Null for installation API keys. */
  accountUsername: string | null
  status: 'running' | 'succeeded' | 'failed' | 'abandoned'
  startedAt: string
  finishedAt?: string | null
  lastError?: string | null
  costUsd?: number | null
  tokens?: TokenUsage | null
}

// A question in the ask. The operator answers it with one of its options.
export interface AskQuestion {
  id: string
  prompt: string
  options: { id: string; label: string; recommended: boolean }[]
}

// A choice the ask offers. An answer with a control that requires a note must carry a
// note or an answer to a question.
export interface AskControl {
  id: string
  note_required: boolean
}

// The ask that a parked run declares, as input_request with snake_case keys.
// ``presentation`` sets where the operator answers. With "in_app", the shell shows the
// controls, the questions, and the artifact. With "external", it links to ``url``.
export interface InputRequest {
  presentation: 'in_app' | 'external'
  label?: string
  url?: string
  controls?: AskControl[]
  questions?: AskQuestion[]
  artifact_id?: string | null
  /** The prose or blocks of the workflow. The shell shows them beside the document. */
  context?: string | Block[]
  /** The reply fields the gate declares. */
  reply_fields?: string[]
}

// The output of a call that the shell shows inside an in-app review.
export interface ArtifactContent {
  kind: string
  title: string
  content: string
}

// One run on the subject's timeline, with its agent calls in execution order.
export interface RunSummary {
  id: string
  // The app that declares the kind of the run. It serves the transcripts of the run.
  app: string
  // The durable kind, such as "field_notes.summarize". ``label`` is its display name
  // from the backend, such as "Summarize".
  kind: string
  label: string
  // The run that this one retries. Null for a first run.
  retryFrom: string | null
  state: RunState
  failure?: string | null
  gate: string | null
  // A retained ask is current only while the run is parked.
  inputRequest?: InputRequest | null
  createdAt: string
  updatedAt: string
  accountUsername: string
  agentCalls: AgentCallSummary[]
}

// One run with its app and its subject. ``subject`` is null for a run about no subject.
export interface RunDetail {
  subject: { type: string; id: string } | null
  run: RunSummary
}

// One row of the active board. It holds the subject summary and the lifecycle status.
export interface SubjectRow<S extends SubjectSummary = SubjectSummary> {
  summary: S
  status: SubjectStatus
}

// The full read of one subject. It holds its summary, its status, and its timeline.
// The timeline lists its runs, oldest first, each with its agent calls.
export interface SubjectResponse<S extends SubjectSummary = SubjectSummary> {
  summary: S
  status: SubjectStatus
  timeline: RunSummary[]
  // The live phase of the driving run while it starts or waits. The shell writes the
  // words. ``phaseEndsAt`` holds the end of a timed phase.
  phase?: string | null
  phaseEndsAt?: string | null
}

export interface ArtifactFile {
  name: string
  sizeBytes: number
  updatedAt: string
}

// The output of a call that the shell shows by its kind, apart from the raw files.
// ``name`` is its file in the call folder. The transcript files route downloads it
// like any other file.
export interface ArtifactDescriptor {
  kind: string
  title: string
  name: string
}

// The files of a call on disk, by role. Each one carries its file name. The client
// builds the download URL with subjectApi.transcriptFile.
export interface AgentCallFiles {
  prompt?: ArtifactFile | null
  stdout?: ArtifactFile | null
  stderr?: ArtifactFile | null
  response?: ArtifactFile | null
  metadata?: ArtifactFile | null
  artifact?: ArtifactDescriptor | null
}

export interface TranscriptChunk {
  callId: string
  stream: 'stdout' | 'stderr'
  offset: number
  nextOffset: number
  eof: boolean
  text: string
}

// One installed app, from the backend registry. The shell builds its navigation and
// generic pages from it. ``hasFrontend`` tells that the package ships its own built
// UI, an ESM module that the shell mounts from /app/<name>. ``navigation`` holds the
// subnav tabs that the app declares, as (url, name) pairs.
export interface App {
  name: string
  icon: string
  description: string
  builtin: boolean
  subjectTypes: string[]
  hasFrontend: boolean
  navigation: [string, string][]
  pages: PageEntry[]
  operations: Operation[]
}

// The page declarations of the app, as the shell sees them. They match
// docs/druks-ui.md. They arrive in the order in which routes match.
export interface PageEntry {
  name: string
  label: string
  path: string
  parent: string
  order: number
  subjectType: string
}

export interface Link {
  block: 'link'
  label: string
  page: string
  arguments: Record<string, string>
  url: string
  subject: Follows | null
}

// Where something stands. The app writes the word. The tone selects the presentation.
export interface StatusValue {
  value: 'status'
  label: string
  tone: 'neutral' | 'active' | 'success' | 'warning' | 'danger'
  link: Link | null
}

export interface TimelineItem {
  when: string
  title: string
  description: string
  status: StatusValue | null
}

export interface ProgressStep {
  label: string
  status: StatusValue
}

export interface TextValue {
  value: 'text'
  text: string
  description: string
  link: Link | null
}

export interface NumberValue {
  value: 'number'
  number: number
  unit: string
  tone: 'neutral' | 'active' | 'success' | 'warning' | 'danger'
}

export interface TimeValue {
  value: 'time'
  when: string | null
  // The word that the shell shows when there is no time.
  empty: string
}

// Where the work on one subject stands. Druks reads the status when it serves
// the page. The shell writes the word.
export interface SubjectStatusValue {
  value: 'subject_status'
  subject: Follows
  working: string
  status?: SubjectStatus
}

export interface ControlsValue {
  value: 'controls'
  controls: Array<Action | Link>
}

// One rendered datum. It reads the same way in Facts, Metrics, List, and Table.
export type Value =
  | TextValue
  | NumberValue
  | StatusValue
  | SubjectStatusValue
  | TimeValue
  | ControlsValue

export interface ChartSeries {
  label: string
  points: number[]
}

export interface Metric {
  label: string
  value: Value
  description: string
}

export interface Fact {
  label: string
  value: Value
}

export interface TableColumn {
  label: string
  align: 'start' | 'end'
}

export interface TableRow {
  cells: Value[]
  detail: string
  key: string
}

export interface CardBlock {
  block: 'card'
  title: string
  description: string
  blocks: Block[]
  controls: (Action | Link)[]
  link?: Link | null
  drag?: Record<string, unknown>
}

export interface EmptyStateBlock {
  block: 'empty_state'
  title: string
  description: string
  controls: (Action | Link)[]
}

export interface ImageBlock {
  block: 'image'
  url: string
  alternativeText: string
  caption: string
}

export interface FileSummary {
  id: string
  name: string
  contentType: string
  size: number
  url: string
}

export interface Option {
  value: string
  label: string
  group?: string
}

interface FieldBase {
  name: string
  label: string
  helpText: string
  isRequired: boolean
}

// One named input. An action collects it before it runs. A page filter
// collects it in the URL query.
export type Field =
  | (FieldBase & { field: 'text'; value: string; placeholder: string })
  | (FieldBase & {
      field: 'text_area'
      value: string
      placeholder: string
      rows: number
      markdown?: boolean
    })
  | (FieldBase & {
      field: 'number'
      value: number | null
      minimum: number | null
      maximum: number | null
      step: number | null
    })
  | (FieldBase & { field: 'select'; options: Option[]; value: string })
  | (FieldBase & {
      field: 'multi_select'
      options: Option[]
      value: string[]
      isSearchable?: boolean
    })
  | (FieldBase & { field: 'radio'; options: Option[]; value: string })
  | (FieldBase & { field: 'checkbox'; value: boolean })
  | (FieldBase & { field: 'upload'; accept: string })
  | (FieldBase & { field: 'multi_upload'; accept: string })
  | (FieldBase & { field: 'secret' })

// A control that calls one of the app's own operations. The shell finds the method
// and the URL of the operation in the roster.
export interface Action {
  block: 'action'
  label: string
  operation: string
  arguments: Record<string, unknown>
  fields: Field[]
  tone: 'default' | 'primary' | 'danger'
  confirm: string
  refresh: 'none' | 'page' | 'region'
  link: Link | null
}

// One route that an Action can call. The list leaves out reads. A GET is not an action.
export interface Operation {
  id: string
  method: string
  path: string
}

export type Block =
  | { block: 'text'; text: string }
  | { block: 'markdown'; text: string }
  | { block: 'quote'; text: string }
  | {
      block: 'section'
      title: string
      name: string
      controls: (Action | Link)[]
      blocks: Block[]
      follows: Follows | null
    }
  | { block: 'gate_controls'; subject: Follows; status: SubjectStatus }
  | { block: 'timeline'; title: string; items: TimelineItem[] }
  | {
      block: 'progress'
      label: string
      completed: number | null
      total: number
      steps: ProgressStep[]
    }
  | ImageBlock
  | { block: 'files'; title: string; files: FileSummary[] }
  | {
      block: 'chart'
      kind: 'line' | 'bar' | 'area' | 'pie'
      title: string
      categories: string[]
      series: ChartSeries[]
      categoryLabel: string
      valueLabel: string
    }
  | { block: 'image_gallery'; title: string; images: ImageBlock[] }
  | { block: 'metrics'; title: string; metrics: Metric[] }
  | { block: 'facts'; title: string; facts: Fact[] }
  | {
      block: 'table'
      title: string
      columns: TableColumn[]
      rows: TableRow[]
      empty: EmptyStateBlock | null
      select: string
      actions: Action[]
    }
  | { block: 'list'; title: string; items: Value[] }
  | { block: 'stack'; gap: 'small' | 'medium' | 'large'; blocks: Block[] }
  | { block: 'columns'; layout?: 'even' | 'sidebar' | 'split'; blocks: Block[] }
  | Action
  | {
      block: 'form'
      title: string
      description: string
      fields: Field[]
      action: Action
      extraActions: Action[]
      submit: 'button' | 'change'
      layout: 'stack' | 'prose' | 'row'
    }
  | CardBlock
  | {
      block: 'cards'
      title: string
      cards: CardBlock[]
      empty: EmptyStateBlock | null
      layout?: 'wrap' | 'stack' | 'tiles'
      drop?: Action | null
    }
  | {
      block: 'callout'
      tone: 'info' | 'success' | 'warning' | 'danger'
      title: string
      text: string
      controls: (Action | Link)[]
    }
  | { block: 'divider' }
  | EmptyStateBlock
  | Link

// The subject that a page or a named region watches. The shell streams it. It
// rereads the page on each snapshot of the stream. An empty ``subjectId`` watches
// every subject of the type, through the board stream.
export interface Follows {
  subjectType: string
  subjectId: string
}

// What a page function returned at one moment. It is the ``Page`` of the contract.
export interface PageSnapshot {
  title: string
  description: string
  controls: (Action | Link)[]
  filters?: Field[]
  blocks: Block[]
  follows: Follows | null
}

// The open gate of a parked run. ``parkedAt`` names the exact question. The answer
// sends it back unchanged.
export interface Gate {
  run: string
  gate: string
  parkedAt: string
  ask: InputRequest
  artifact: ArtifactContent | null
}

export interface GateAnswer {
  parkedAt: string
  control: string
  answers: Record<string, string>
  note: string
}


export type AgentEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

/** One entry of the model picker. It holds the model id of the provider and its label. */
export interface CatalogModel {
  id: string
  label: string
}

/** The provider and the billing options of a registered harness. */
export interface Harness {
  name: string
  /** Null for a key-only CLI. */
  provider: string | null
  billingOptions: string[]
}

/** One model provider, which answers and bills a request. An account holds at
 * most one credential for each provider. Every harness that drives the provider
 * uses that credential. */
export interface Provider {
  id: string
  label: string
  billingOptions: string[]
  /** `login` runs the sign-in flow of the provider. `token` takes a pasted token. Empty for a key-only provider. */
  signIn: string
}

/** The model list of one provider. `label` names the provider. */
export interface ProviderCatalog {
  provider: string
  label: string
  models: CatalogModel[]
  fetchedAt: string
}

/** One Models.dev provider that runs on a single API key. */
export interface ProviderDirectoryEntry {
  provider: string
  label: string
  documentationUrl: string | null
  apiUrl: string | null
  models: CatalogModel[]
}

/** The requester's subscription at a provider. */
export interface ProviderSubscription {
  provider: string
  /** The email that the provider gave at connect. It is for display only, never for access. Null for a pasted token. */
  providerEmail: string | null
  expiresAt: string | null
  /** When the person must sign in again. Null when the provider names no end. */
  refreshTokenExpiresAt: string | null
  updatedAt: string
  lastRefreshedAt: string | null
  revokedAt: string | null
  revokedReason: string
  // False after the token expires or after a revocation.
  connected: boolean
}

/** The installation's API key at a provider. */
export interface ProviderKey {
  provider: string
  keyTail: string
  updatedBy: Account
  updatedAt: string
}

export interface Account {
  id: string
  username: string
  isDefault: boolean
  role: AccountRole
}

export type AccountRole = 'admin' | 'member'

export interface Team {
  id: string
  name: string
  members: Account[]
  apps: string[]
}

export interface TeamInput {
  name: string
  memberIds: string[]
  apps: string[]
}

export interface AgentInput {
  name: string
  description: string
  instructions: string
  skills: string[]
  harness: string
  model: string
  isPrivate: boolean
  ownerId: string
  channelId: string | null
  // The id of an uploaded png, jpg or gif.
  picture: string | null
}

export interface Agent extends Omit<AgentInput, 'picture'> {
  id: string
  picture: FileSummary | null
}

// A schedule set in a conversation of the agent. ``lastRun`` is the run of its newest due time.
export interface AgentSchedule {
  id: string
  accountId: string
  conversationId: string
  tool: string
  arguments: Record<string, unknown>
  cron: string
  nextRunAt: string
  lastRun: ScheduledRun | null
}

/** What /api/auth/me answers. It tells how this deployment authenticates, and which
 * account sent the request. The account is null in mode none before the first account
 * exists. It also tells whether that account still needs its first provider credential. */
export interface Identity {
  authMode: 'none' | 'header' | 'jwt'
  account: Account | null
  onboardingRequired: boolean
}

/** connectionId is the opaque id of this connect attempt. The client sends it back.
 * Two connects at the same time then keep their own pending state. */
export type ConnectChallenge =
  | { method: 'code'; connectionId: string; authorizeUrl: string }
  | {
      method: 'device'
      connectionId: string
      authorizeUrl: string
      userCode: string
      pollInterval: number
    }

export interface ServiceField {
  name: string
  label: string
  help: string
  type: string
  multiline: boolean
  isRequired: boolean
}

/** One signed-in provider account. The person who consented owns it. */
export interface Connection {
  id: string
  provider: string
  scopes: string[] | null
  identity: Record<string, unknown> & {
    subject?: string
    authority?: string
    source?: string
    email?: string
    email_verified?: boolean
    name?: string
    login?: string
    username?: string
    subscription?: string
  }
  identityStatus: 'resolved' | 'unavailable' | 'failed' | null
  identityError: string | null
  connectedAt: string
  revokedAt: string | null
  revokedReason: string
}

/** A linked WhatsApp number, which is one WAHA session. ``admin`` is the name of the person who
 * sent its one-time code. */
export interface WahaSession {
  id: string
  number: string | null
  name: string | null
  admin: string | null
  isPhonePaired: boolean
  identityStatus: 'resolved' | 'unavailable' | 'failed' | null
  revokedAt: string | null
  revokedReason: string
}

/** A sender on a channel with no person behind it, such as another app's bot or a workflow. */
export interface Automation {
  id: string
  name: string
  isAccepted: boolean
}

/** A Twilio number linked to an app's Bot. A removed one keeps its calls. */
export interface LinkedNumber {
  id: string
  number: string
  revokedAt: string | null
}

/** A call to a linked number. It lasts from the ring to its last line. A caller who hides
 * their number has no `caller`. */
export interface Call {
  id: string
  caller: string
  createdAt: string
  lastLineAt: string
}

/** A line of a call. It is what the caller or the assistant said. */
export interface CallLine {
  id: string
  role: 'user' | 'assistant'
  text: string
}

/** A Twilio number that is not linked yet. */
export interface UnlinkedNumber {
  sid: string
  number: string
}

/** One declared service. It is the registered app of this installation at an
 * outside provider. ``facts`` hold identity only. Stored secrets never leave the
 * backend. */
export interface Service {
  slug: string
  title: string
  description: string
  required: boolean
  connected: boolean
  managed: boolean
  managedBy: string
  facts: Record<string, string | string[]>
  connectedAt: string | null
  fields: ServiceField[]
  createUrl: string
  installUrl: string
  isOauth: boolean
  scopes: string[]
  usedBy: string[]
  connections: Connection[]
}


export type Billing = 'subscription' | 'api_key'

export interface PersonalSettings {
  timezone: string
  theme: 'system' | 'light' | 'dark'
  notificationChannelId: string | null
  isTagRequired: boolean
}

/** A Slack channel that the bot is a member of. A person can send their notifications to it. */
export interface NotificationChannel {
  id: string
  name: string
}

export interface NotificationResponse {
  id: string
  body: string
  deepLink: string | null
  readAt: string | null
  createdAt: string
}

export interface InstallationSettings {
  updatedAt: string
  defaultHarness: string
  defaultModel: string
  defaultBilling: Billing
  defaultEffort: string
  fastMode: boolean
  includeApps: boolean
  defaultTimeout: number
}

export interface UpdatePersonalSettingsRequest {
  timezone?: string
  theme?: PersonalSettings['theme']
  notificationChannelId?: string | null
  isTagRequired?: boolean
}

export interface UpdateSettingsRequest {
  defaultHarness?: string
  defaultModel?: string
  defaultBilling?: Billing
  defaultEffort?: string
  fastMode?: boolean
  includeApps?: boolean
  defaultTimeout?: number
}

export type BrowserSessionStatus = 'needs_login' | 'ready' | 'stale' | 'anonymous'
export type BrowserSessionPayloadFormat = 'storage_state' | 'profile_dir'

export interface BrowserSession {
  name: string
  status: BrowserSessionStatus
  /** Stored facts arrive with the row. A declared session that nobody used yet
   * has none. */
  payloadFormat: BrowserSessionPayloadFormat | null
  site: string
  /** False for a leftover row of an app that is gone. */
  isDeclared: boolean
  createdAt: string | null
  lastRefreshedAt: string | null
  lastUsedAt: string | null
}

export type Source = 'agent' | 'default'
/** A timeout can also be the declared value of the agent. */
export type TimeoutSource = 'agent' | 'declared' | 'default'

export interface InstructionSetting {
  /** The durable id of the agent, `<app>.<attribute>`. It is the key in the agent maps of an update. */
  name: string
  /** The declared display name, else the attribute name. */
  label: string
  /** A short text that says what the agent does. */
  description: string
  harness: string
  harnessSource: Source
  model: string
  source: Source
  billing: Billing
  billingSource: Source
  effort: string
  effortSource: Source
  /** Run timeout in seconds. */
  timeout: number
  timeoutSource: TimeoutSource
}

export interface WorkflowSettingField {
  name: string
  /** The label and the one-line help of the field, from its Field(title=, description=). */
  label: string
  help: string
  /** The kind that selects the input control: bool, int, str, enum, secret, or cron. */
  type: string
  value: unknown
  default: unknown
  /** The allowed values of an enum field. Null for every other kind. */
  choices: string[] | null
  choiceDetails: Record<string, { label: string; help: string; group?: string }>
  /** The heading that this field groups under. Empty for a field with no group. */
  section: string
  /** The sibling field and the values of it that show this field. The name is empty
   * when the shell always shows the field. */
  visibleWhenField: string
  visibleWhenValues: unknown[]
  /** For a secret field, whether Druks stores a value now. Null for every other field. */
  secretSet: boolean | null
  /** The value has newlines that matter, such as a pasted PEM. The shell shows a text area. */
  multiline: boolean
  overridden: boolean
}

export interface WorkflowSettings {
  kind: string
  fields: WorkflowSettingField[]
}

export interface AppSettings {
  name: string
  description: string
  /** The Lucide icon name of the rail glyph, from APP_ICONS. An unknown name gets the default icon. */
  icon: string
  /** True for a platform app. The shell leaves it out of the app roster. */
  builtin: boolean
  /** The id of the app's Bot, which answers people on its channels. */
  bot: string | null
  botAccess: 'public' | 'private' | null
  instructions: InstructionSetting[]
  workflows: WorkflowSettings[]
  /** The settings of the app itself, not of a workflow. */
  settings: WorkflowSettingField[]
}

/** The live choices of an app's settings, keyed by field name. */
export type AppSettingChoices = Record<string, { value: string; label: string; group?: string }[]>

export interface AppsSettingsResponse {
  allowedEfforts: string[]
  apps: AppSettings[]
  /** The chat channels that a Bot answers on now. They are the registered channels with a connected service. */
  channels: string[]
}

export type AppSettingsProblems = Record<string, Record<string, string>>

export interface UpdateAppsSettingsRequest {
  agentHarnesses?: Record<string, string | null>
  agentModels?: Record<string, string | null>
  agentBillings?: Record<string, Billing | null>
  agentEfforts?: Record<string, string | null>
  agentTimeouts?: Record<string, number | null>
  /** Keyed by workflow kind. */
  workflowSettings?: Record<string, Record<string, unknown>>
  /** Keyed by app name. */
  appSettings?: Record<string, Record<string, unknown>>
}


export interface EventTopic {
  app: string
  topic: string
}

export interface EventFilters {
  q?: string
  app?: string
  topic?: string
  from?: string
  until?: string
}

export interface FeedItem {
  id: string
  seq: number
  at: string
  // A lifecycle topic, such as "workflow.finished", or a milestone that an app
  // recorded, such as "merged". This client writes the words in lib/feed.
  topic: string
  app?: string | null
  subjectType?: string | null
  subjectId?: string | null
  subjectKey?: string | null
  payload: {
    [fact: string]: unknown
    kind?: string | null
    run?: string | null
    gate?: string | null
    input_requested_at?: string | null
    input_request?: InputRequest | null
    result?: Record<string, unknown> | null
    summary?: string | null
    failure?: string | null
    reason?: string | null
    artifact_id?: string | null
  }
}

export interface FeedResponse {
  items: FeedItem[]
  // The snapshot the live stream continues from.
  cursor: string
  nextCursor: string | null
}

// Whether the subject, run, and artifact that one Activity row recorded still exist.
export interface FeedDestinations {
  isSubjectAvailable: boolean
  isRunAvailable: boolean
  isArtifactAvailable: boolean
}


export interface UsageMetric {
  percentLeft: number | null
  resetsAt: string | null
  model: string | null
}

export interface UsageProviderSummary {
  // The panels, colors, and legends use the registered provider id as a key.
  id: string
  label: string
  available: boolean
  /** True when the requesting account has its own connection. False shows a connect action. */
  connected: boolean
  providerEmail: string | null
  planTier: string | null
  fiveHour: UsageMetric | null
  weeks: UsageMetric[]
  mainLimitReached: boolean | null
  reserve: UsageMetric | null
  // The window metrics of an unmetered plan stay full. The shell shows "unmetered" and
  // the real use, not a quota bar that never moves.
  unlimited: boolean
  scrapedAt: string | null
  ageSeconds: number | null
  stale: boolean
  error: string | null
  rawOutput: string | null
}

export interface UsageResponse {
  providers: UsageProviderSummary[]
}

export interface UsageHistoryPoint {
  t: string
  pct: number
}

export interface UsageWindowHistory {
  model: string | null
  points: UsageHistoryPoint[]
}

export interface UsageProviderHistory {
  id: string
  fiveHour: UsageHistoryPoint[]
  weeks: UsageWindowHistory[]
  reserve: UsageHistoryPoint[]
}

export interface UsageHistoryResponse {
  providers: UsageProviderHistory[]
}

export interface UsageProviderToday {
  id: string
  /** The spend of the viewer's own runs on their subscription. */
  spendUsd: number
  /** The spend of all runs on the API key of the installation. */
  keySpendUsd: number
  tokens: number
  runs: number
  // The spend in each local hour, 24 buckets for the histogram.
  hours: number[]
}

export interface UsageTodayResponse {
  day: string
  timezone: string
  providers: UsageProviderToday[]
}

export const ALLOWED_EFFORTS: readonly AgentEffort[] = ['low', 'medium', 'high', 'xhigh', 'max']

export interface Skill {
  name: string
  description: string
  enabled: boolean
  updatedAt: string
}

export interface SkillCollection {
  id: string
  source: string
  name: string
  updatedAt: string
  skills: Skill[]
}

export interface McpServerDirectoryEntry {
  // One server that the dashboard offers to add. ``name`` is the name of the installed row.
  name: string
  title: string
  description: string
  url: string
}

// A personal access token that an agent sends as `Authorization: Bearer …` to call
// this API. Only the prefix shows here. Druks returns the plaintext once, when it
// creates the token.
export interface Pat {
  id: string
  name: string
  prefix: string
  createdAt: string
  expiresAt: string
  lastUsedAt: string | null
  revokedAt: string | null
  status: 'active' | 'expired' | 'revoked'
}

export interface McpServer {
  name: string
  url: string
  isEnabled: boolean
  isOauth: boolean
  identityMode: string | null
  // The scopes that a connect requests. Null requests what the server advertises.
  scopes: string[] | null
  // A server from the catalog. Druks manages it. A person can disable it but not
  // remove it.
  builtin: boolean
  // The raw token never leaves the backend. ``hasToken`` only tells whether a token
  // is set.
  hasToken: boolean
  // The source of the credential that this account uses for the server.
  credential: McpCredential
  // The service that owns the server's host.
  service: string | null
}

export type McpCredential = 'headers' | 'grant' | 'service_connection' | 'service_login'

export interface McpServerScopes {
  // What the server advertises, and what a connect requests. Null requests the
  // advertised list.
  advertised: string[]
  requested: string[] | null
}

export interface McpServerConnection {
  // Null names the shared connection that every account uses.
  accountUsername: string | null
}
