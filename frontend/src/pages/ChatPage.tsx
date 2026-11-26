import { useEffect, useReducer, useRef, useState } from 'react'
import type { ToolCall } from '@agentclientprotocol/sdk'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown, ArrowLeft, ArrowUp, Check, ChevronRight, MessageSquare, Pin, Plus, Search, Settings, X,
} from 'lucide-react'
import { useStickToBottom } from 'use-stick-to-bottom'
import { Link, useLocation } from 'wouter'

import { api, ApiError, UnauthorizedError } from '../api/client'
import type { Account, Field } from '../api/types'
import { appLabel } from '../apps/registry'
import { chatApi } from '../chat/api'
import {
  compareConversations, conversationReducer, conversationTitle, formAnswer, formErrors, initialConversation, questionAnswer, savedReplyRows, threadRows,
  type ActiveRun, type Conversation, type ConversationAction, type ConversationSummary, type Message, type Question, type ReplyBlock, type ReplyRow,
} from '../chat/state'
import { Button, Select } from '../components/Control'
import { Markdown } from '../components/Markdown'
import { Page } from '../components/Page'
import { AppPagesProvider } from '../druksui/AppPage'
import { AppSurface } from '../druksui/AppSurface'
import { Blocks } from '../druksui/Blocks'
import { Fields } from '../druksui/Fields'
import { useFieldState } from '../druksui/Form'
import { HasTextFormContext } from '../druksui/pages'
import { PageHeading } from '../components/PageHeading'
import { ConnectionCards } from '../components/ConnectionCards'
import { CurrentMilestoneCard } from '../components/OnboardingProgress'
import { useFormatters } from '../lib/preferences'
import { zonedParts } from '../lib/format'
import { phaseLine } from '../lib/phase'
import '../chat.css'

const conversationListKey = ['chat', 'conversations']
const conversationIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const stoppedTurns: Partial<Record<NonNullable<Message['state']>, { title: string, detail: string }>> = {
  interrupted: { title: 'Interrupted', detail: 'This turn stopped before it finished.' },
  cancelled: { title: 'Cancelled', detail: 'You stopped this turn.' },
  failed: { title: 'Failed', detail: 'Druks could not start this turn.' },
}

export function ChatPage({ id, account }: { id?: string; account: Account }) {
  const [, navigate] = useLocation()
  const queryClient = useQueryClient()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [agentId, setAgentId] = useState('')
  const [search, setSearch] = useState('')
  const [now, setNow] = useState(() => new Date())
  const list = useQuery({ queryKey: conversationListKey, queryFn: chatApi.list, refetchInterval: 10_000 })
  const pin = useMutation({
    mutationFn: ({ id, isPinned }: { id: string; isPinned: boolean }) => chatApi.setPinned(id, isPinned),
    onSuccess: (summary) => {
      queryClient.setQueryData<ConversationSummary[]>(conversationListKey, (current = []) =>
        current.map((conversation) => conversation.id === summary.id ? summary : conversation).sort(compareConversations),
      )
      void queryClient.invalidateQueries({ queryKey: conversationListKey })
    },
  })
  const format = useFormatters()
  const draftKey = id ?? 'new'
  const conversationId = id && id !== 'new' ? id : undefined
  const today = zonedParts(now, format.timezone)
  const todayDate = Date.UTC(today.year, today.month - 1, today.day)
  const groups = new Map<string, ConversationSummary[]>([
    ['Pinned', []], ['Today', []], ['Yesterday', []], ['Earlier', []],
  ])
  const matches = [...list.data ?? []]
    .filter((conversation) => conversationTitle(conversation).toLowerCase().includes(search.trim().toLowerCase()))
    .sort(compareConversations)
  for (const conversation of matches) {
    const day = zonedParts(new Date(conversation.lastMessageAt), format.timezone)
    const daysAgo = (todayDate - Date.UTC(day.year, day.month - 1, day.day)) / 86_400_000
    let group: string
    if (conversation.isPinned) group = 'Pinned'
    else if (daysAgo === 0) group = 'Today'
    else if (daysAgo === 1) group = 'Yesterday'
    else group = 'Earlier'
    groups.get(group)!.push(conversation)
  }

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(timer)
  }, [])

  function togglePinned(conversation: ConversationSummary) {
    pin.mutate({ id: conversation.id, isPinned: !conversation.isPinned })
  }

  if (conversationId !== undefined && !conversationIdPattern.test(conversationId)) {
    return <Page inset><p role="alert">Conversation not found.</p><Link href="/chat">Back to conversations</Link></Page>
  }

  return (
    <Page
      className={`page-chat${id ? ' has-conversation' : ''}`}
      scroll="internal"
      inset
      header={
        <PageHeading
          title="Chat"
          icon={<MessageSquare aria-hidden="true" />}
          actions={account.role === 'admin' ? (
            <Link
              href="/apps/chat/settings"
              className="druks-button druks-button--compact"
              aria-label="Chat Settings"
              title="Chat Settings"
            >
              <Settings size={16} aria-hidden="true" />
              Chat Settings
            </Link>
          ) : undefined}
        />
      }
    >
      {pin.isError && <div className="chat-pin-error" role="alert">
        <span>Could not save the pin.</span>
        <button className="druks-button druks-button--compact" onClick={() => pin.mutate(pin.variables)}>Try again</button>
        <button className="druks-button druks-button--compact druks-button--icon-only" aria-label="Dismiss pin error" onClick={() => pin.reset()}><X size={16} /></button>
      </div>}
      <aside className="chat-list druks-panel" aria-label="Conversations">
        <header className="chat-list-head">
          <label className="chat-search druks-field druks-field--compact">
            <Search size={15} aria-hidden="true" />
            <input type="search" aria-label="Search conversations" placeholder="Search conversations" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <Link href="/chat/new" className="druks-button druks-button--primary druks-button--compact druks-button--icon-only" aria-label="New conversation" title="New conversation">
            <Plus size={19} aria-hidden="true" />
          </Link>
        </header>
        <div className="chat-list-body">
          {list.isPending && <p className="chat-notice" role="status">Loading conversations…</p>}
          {list.isError && <div className="chat-notice" role="alert">
            <p>Could not load conversations.</p>
            <button className="druks-button druks-button--compact" onClick={() => void list.refetch()}>Try again</button>
          </div>}
          {list.isSuccess && list.data.length === 0 && <p className="chat-notice">Your conversations will appear here.</p>}
          {list.isSuccess && list.data.length > 0 && matches.length === 0 && <div className="chat-notice" role="status">
            <p>No matching conversations.</p>
            <button className="druks-button druks-button--compact" onClick={() => setSearch('')}>Clear search</button>
          </div>}
          {Array.from(groups, ([day, conversations]) => conversations.length > 0 && <section key={day} aria-label={day}>
            <h2 className="chat-day">{day}</h2>
            {conversations.map((conversation) => <div className="chat-list-row" key={conversation.id} data-selected={conversation.id === conversationId}>
              <Link href={`/chat/${conversation.id}`} className="chat-list-item" aria-current={conversation.id === conversationId ? 'page' : undefined}>
                <span className="chat-list-title" title={conversationTitle(conversation)}>{conversationTitle(conversation)}</span>
                <time dateTime={conversation.lastMessageAt} title={format.absTime(conversation.lastMessageAt)}>
                  {format.absTimeCompact(conversation.lastMessageAt)}
                </time>
                {conversation.activeMessageId
                  ? <span className="chat-list-status"><span className="chat-activity-dot" />{conversation.agentName ?? 'Agent'} is replying</span>
                  : <span className="chat-list-status">{conversation.messageCount} {conversation.messageCount === 1 ? 'message' : 'messages'}
                    {conversation.agentName && <> <span aria-hidden="true">/</span> {conversation.agentName}</>}
                  </span>}
              </Link>
              <PinButton conversation={conversation} onPin={togglePinned} disabled={pin.isPending} />
            </div>)}
          </section>)}
        </div>
      </aside>
      <ConversationThread
        key={draftKey}
        id={conversationId}
        account={account}
        showMilestone={id === undefined}
        summary={list.data?.find((conversation) => conversation.id === conversationId)}
        onPin={togglePinned}
        pinPending={pin.isPending}
        draft={drafts[draftKey] ?? ''}
        onDraft={(draft) => setDrafts((current) => ({ ...current, [draftKey]: draft }))}
        agentId={agentId}
        onAgent={setAgentId}
        onCreated={(conversation, submitted) => {
          setDrafts((current) => ({ ...current,
            [conversation.id]: current[draftKey] === submitted ? '' : current[draftKey] ?? '',
            [draftKey]: '',
          }))
          setAgentId('')
          navigate(`/chat/${conversation.id}`)
        }}
      />
    </Page>
  )
}

function PinButton({ conversation, onPin, disabled }: {
  conversation: ConversationSummary
  onPin: (conversation: ConversationSummary) => void
  disabled: boolean
}) {
  const label = `Pin ${conversationTitle(conversation)}`
  return <Button variant="quiet-icon-toggle" size="compact" iconOnly aria-label={label} title={label} aria-pressed={conversation.isPinned} disabled={disabled} onClick={() => onPin(conversation)}>
    <Pin size={16} fill={conversation.isPinned ? 'currentColor' : 'none'} aria-hidden="true" />
  </Button>
}

function ConversationThread({ id, account, showMilestone, summary, onPin, pinPending, draft, onDraft, agentId, onAgent, onCreated }: {
  id?: string
  account: Account
  showMilestone: boolean
  summary?: ConversationSummary
  onPin: (conversation: ConversationSummary) => void
  pinPending: boolean
  draft: string
  onDraft: (draft: string) => void
  agentId: string
  onAgent: (agentId: string) => void
  onCreated: (conversation: Conversation, submitted: string) => void
}) {
  const queryClient = useQueryClient()
  const [state, dispatch] = useReducer(conversationReducer, initialConversation)
  const [connection, setConnection] = useState<'opening' | 'connected' | 'reconnecting' | 'denied'>('opening')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [stopping, setStopping] = useState(false)
  const mounted = useRef(true)
  const draftRef = useRef(draft)
  const composer = useRef<HTMLTextAreaElement>(null)
  const format = useFormatters()
  const apps = useQuery({ queryKey: ['apps'], queryFn: api.listApps, enabled: !id, staleTime: 60_000 })
  const agents = useQuery({ queryKey: ['agents'], queryFn: api.agents, enabled: !id })
  // An admin sees every agent. Only its owner talks to a private agent.
  const talkableAgents = (agents.data ?? []).filter((agent) => !agent.isPrivate || agent.ownerId === account.id)
  const chosenAgent = talkableAgents.find((agent) => agent.id === agentId)
  // A run's state sends no events. The page reads its active runs again.
  const activeRuns = useQuery({ queryKey: ['chat', 'active-runs', id], queryFn: () => chatApi.listActiveRuns(id!), enabled: Boolean(id), refetchInterval: 2_000 })
  // A report sends no events either. The page reads the run statuses again.
  const statuses = useQuery({ queryKey: ['chat', 'statuses', id], queryFn: () => chatApi.listStatuses(id!), enabled: Boolean(id), refetchInterval: 2_000 })
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({ initial: 'instant', resize: 'instant' })

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    draftRef.current = draft
  }, [draft])

  useEffect(() => {
    if (!id) return
    let active = true
    let socket: WebSocket | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempts = 0

    function receive(action: ConversationAction) {
      if (!active) return
      dispatch(action)
      if (action.type === 'snapshot') {
        const summary: ConversationSummary = {
          id: action.id, title: action.title, source: action.source, userId: action.userId,
          userName: action.userName, createdAt: action.createdAt,
          isPinned: action.isPinned, lastMessageAt: action.lastMessageAt,
          messageCount: action.messageCount, activeMessageId: action.activeMessageId,
          agentId: action.agentId, agentName: action.agentName,
        }
        queryClient.setQueryData<ConversationSummary[]>(conversationListKey, (current = []) =>
          [...current.filter((item) => item.id !== summary.id), summary].sort(compareConversations),
        )
      }
    }

    function retry() {
      if (active) {
        setConnection('reconnecting')
        timer = setTimeout(() => void connect(), Math.min(1000 * 2 ** attempts++, 10_000))
      }
    }

    async function connect() {
      try {
        const conversation = await chatApi.get(id!)
        if (!active) return
        receive({ type: 'snapshot', ...conversation })
        const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
        socket = new WebSocket(`${scheme}://${window.location.host}/api/chat/conversations/${id}/ws`)
        socket.onopen = () => {
          if (active) {
            attempts = 0
            setConnection('connected')
            setError('')
          }
        }
        socket.onmessage = (event) => receive(JSON.parse(event.data) as ConversationAction)
        socket.onclose = (event) => {
          if (!active) return
          if (event.code === 1008) {
            setConnection('denied')
            setError('Chat access was denied. Reload the page to check your account.')
          } else retry()
        }
      } catch (caught) {
        if (!active) return
        if (caught instanceof UnauthorizedError || (caught instanceof ApiError && (caught.status === 404 || caught.status === 403))) {
          setConnection('denied')
          setError(caught.message)
        } else retry()
      }
    }

    void connect()
    return () => {
      active = false
      clearTimeout(timer)
      socket?.close()
    }
  }, [id, queryClient])

  async function send(body = draft, originalMessage?: Message) {
    if ((!body.trim() && !originalMessage) || sending) return
    setSending(true)
    setError('')
    try {
      if (id) {
        const message = originalMessage ? await chatApi.retry(id, originalMessage.id) : await chatApi.send(id, body)
        if (!mounted.current) return
        dispatch({ type: 'saved', message })
        if (!originalMessage && draftRef.current === body) onDraft('')
        composer.current?.focus()
        void scrollToBottom()
      } else {
        const conversation = await chatApi.create(body, chosenAgent?.id)
        void queryClient.invalidateQueries({ queryKey: conversationListKey })
        if (mounted.current) onCreated(conversation, body)
      }
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      if (mounted.current) setSending(false)
    }
  }

  async function stop(messageId: string) {
    setStopping(true)
    setError('')
    try {
      await chatApi.stop(id!, messageId)
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      if (mounted.current) setStopping(false)
    }
  }

  const conversation = state.conversation
  const messages = conversation?.messages ?? []
  // A run whose status shows needs no line of its own.
  const reportedRuns = new Set(statuses.data?.map((status) => status.run))
  const replies = new Map(messages.filter((message) => message.role === 'assistant').map((message) => [message.replyTo, message]))
  const userMessages = messages.filter((message) => message.role === 'user')
  const lastReply = messages.findLast((message) => message.role === 'assistant')
  // A channel's turn can answer several messages. Its newest delivered message names the turn.
  const unansweredMessage = userMessages.findLast((message) => message.state === 'delivered')
    ?? userMessages.find((message) => message.state === 'pending')

  return <section className={`chat-thread${id ? ' is-conversation' : ' is-new'}`} aria-label="Conversation">
    {id && <header className="chat-thread-head">
      <Link href="/chat" className="chat-back druks-button druks-button--compact druks-button--icon-only" aria-label="Back to conversations"><ArrowLeft size={16} /></Link>
      <div className="chat-thread-title">
        {id && <h3>{conversation ? conversationTitle(conversation) : 'Conversation'}</h3>}
        {conversation && <p>{conversation.messageCount} {conversation.messageCount === 1 ? 'message' : 'messages'}
          {conversation.agentName && <> <span aria-hidden="true">/</span> {conversation.agentName}</>}
          {lastReply && <> <span aria-hidden="true">/</span> last reply <time dateTime={lastReply.createdAt} title={format.absTime(lastReply.createdAt)}>{format.absTimeCompact(lastReply.createdAt)}</time></>}
        </p>}
      </div>
      {id && connection === 'reconnecting' && <span className="chat-connection" role="status">Reconnecting…</span>}
      {conversation && <PinButton conversation={summary ?? conversation} onPin={onPin} disabled={pinPending} />}
    </header>}
    {!id && <div className="chat-intro">
      <Link href="/chat" className="chat-back druks-button druks-button--compact druks-button--icon-only" aria-label="Back to conversations"><ArrowLeft size={16} /></Link>
      <header className="chat-intro-heading">
        <h2>What should Druks do?</h2>
        <p>Ask about activity, usage, or schedules. Tell Druks what to do next.</p>
      </header>
      {talkableAgents.length > 0 && <label className="chat-agent-picker">Talk to
        <Select value={agentId} onChange={(event) => onAgent(event.target.value)}>
          <option value="">Druks</option>
          {talkableAgents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
        </Select>
      </label>}
      {chosenAgent && <p>{chosenAgent.description}</p>}
    </div>}
    {!id && <ConnectionCards>{showMilestone && <CurrentMilestoneCard />}</ConnectionCards>}
    {!id && <div className="chat-starters" role="group" aria-label="Suggested messages">
      {['What needs my attention?', 'What failed in the last 24 hours?', 'Compare usage this week with last week.'].map((prompt) => <Button key={prompt} variant="quiet" onClick={() => { onDraft(prompt); composer.current?.focus() }}>
        {prompt}
      </Button>)}
    </div>}
    <div className="chat-messages" ref={scrollRef} role="log" aria-label="Messages" aria-live="polite" hidden={!id}>
      <div className="chat-messages-content" ref={contentRef}>
        {id && !conversation && connection === 'opening' && <p className="chat-notice" role="status">Loading conversation…</p>}
        {threadRows(messages, statuses.data ?? []).map((row) => {
          if (row.type === 'status') return <article className="chat-turn" key={row.status.id} aria-label="Run status">
            <div className="chat-run-status">
              <AppPagesProvider app={row.status.app}><Blocks blocks={row.status.blocks} /></AppPagesProvider>
            </div>
          </article>
          const message = row.message
          if (message.role === 'assistant') {
            // A reply shows in the turn of the message it answers. A reminder answers none.
            return <article className="chat-turn" key={message.id} aria-label="Agent message">
              <div className="chat-reply">
                <div className="druks-chat-message">
                  <div className="druks-chat-message__meta">{conversation?.agentName ?? 'Agent'}
                    <time dateTime={message.createdAt} title={format.absTime(message.createdAt)}>{format.absTimeCompact(message.createdAt)}</time>
                  </div>
                  <div className="druks-chat-message__content"><Reply rows={savedReplyRows(message)} /></div>
                </div>
              </div>
            </article>
          }
          const reply = message.state === 'replied' || (message.state === 'cancelled' && message.deliveredAt)
            ? replies.get(message.id) : undefined
          const rows = reply ? savedReplyRows(reply) : state.turns[message.id]?.rows ?? []
          const isQueued = message.state === 'pending' && unansweredMessage?.id !== message.id
          const isWaiting = unansweredMessage?.id === message.id && rows.length === 0 && !state.error
          const stoppedTurn = message.state && stoppedTurns[message.state]
          const hasReplyBlock = rows.length > 0 || reply?.blocks.length || isWaiting || stoppedTurn || reply?.question
          // An internal message is for the agent. The page draws only the agent's side of its turn.
          if (message.isInternal && !hasReplyBlock) return null
          return <article className="chat-turn" key={message.id} aria-label={message.isInternal ? 'Agent message' : 'Message and reply'}>
            {!message.isInternal && <div className="druks-chat-message chat-user-message druks-chat-message--user">
              <div className="druks-chat-message__meta">
                <span>You</span>
                {isQueued && <span className="druks-badge druks-badge--tinted druks-badge--info">Queued</span>}
                <time dateTime={message.createdAt} title={format.absTime(message.createdAt)}>{format.absTimeCompact(message.createdAt)}</time>
              </div>
              <div className="druks-chat-message__content">
                {message.body && <div className="chat-user-body">{message.body}</div>}
                {message.transcript && <div className="chat-user-body chat-user-transcript">{message.transcript}</div>}
                {message.file && <a href={message.file.url} target="_blank" rel="noreferrer">{message.file.name}</a>}
              </div>
            </div>}
            {hasReplyBlock && <div className="chat-reply">
              <div className="druks-chat-message">
                <div className="druks-chat-message__meta">
                  <span className="chat-agent-status">{conversation?.agentName ?? 'Agent'}
                    {stoppedTurn && <span className={`druks-badge druks-badge--tinted${message.state === 'failed' ? ' druks-badge--danger' : message.state === 'interrupted' ? ' druks-badge--warning' : ''}`}>{stoppedTurn.title}</span>}
                  </span>
                  {reply && <time dateTime={reply.createdAt} title={format.absTime(reply.createdAt)}>{format.absTimeCompact(reply.createdAt)}</time>}
                </div>
                <div className="druks-chat-message__content">
                  <Reply rows={rows} />
                  {reply && <ReplyBlocks blocks={reply.blocks} />}
                  {reply?.question && <ReplyQuestion question={reply.question} answer={questionAnswer(messages, reply)} sending={sending} onAnswer={(body) => void send(body)} />}
                  {isWaiting && <p className="chat-waiting" role="status"><span className="chat-activity-dot" />{message.state === 'delivered' ? `${conversation?.agentName ?? 'Agent'} is replying…` : 'Connecting…'}</p>}
                  {stoppedTurn && <div className="chat-interrupted" role="status">
                    <p>{stoppedTurn.detail}</p>
                    <div className="chat-interrupted-actions">
                      <span>Druks never resends by itself.</span>
                      <button className="druks-button druks-button--compact" onClick={() => void send(message.body, message)} disabled={sending}>Send again</button>
                    </div>
                  </div>}
                </div>
              </div>
            </div>}
          </article>
        })}
        <div className="chat-runs" role="status">
          {activeRuns.data?.filter((run) => !reportedRuns.has(run.id)).map((run) => <p className="chat-waiting" key={run.id}>
            <span className="chat-activity-dot" />{runLine(run, format.timezone)}
          </p>)}
        </div>
      </div>
    </div>
    {!isAtBottom && messages.length > 0 && <div className="chat-jump">
      <button className="druks-button druks-button--compact" onClick={() => void scrollToBottom()}><ArrowDown size={16} aria-hidden="true" /> Latest message</button>
    </div>}
    {(error || state.error) && <div className="chat-error" role="alert">
      <p>{error || state.error?.detail}</p>
    </div>}
    <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void send() }}>
      <div className="chat-composer-controls">
        <textarea
          ref={composer}
          aria-label="Message"
          aria-describedby="chat-send-help"
          placeholder={id ? `Reply, or ask ${conversation?.agentName ?? 'Druks'} to do something…` : `Message ${chosenAgent?.name ?? 'Druks'}…`}
          rows={1}
          value={draft}
          onChange={(event) => { draftRef.current = event.target.value; onDraft(event.target.value) }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              void send()
            }
          }}
        />
        <div className="chat-composer-actions">
          {unansweredMessage && !state.error && <button type="button" className="druks-button druks-button--compact" onClick={() => void stop(unansweredMessage.id)} disabled={stopping}>{stopping ? 'Stopping…' : 'Stop'}</button>}
          <button type="submit" className="druks-button druks-button--primary druks-button--compact druks-button--icon-only" aria-label={sending ? 'Sending…' : 'Send'} title="Send" disabled={!draft.trim() || sending || (Boolean(id) && !conversation)}><ArrowUp size={18} aria-hidden="true" /></button>
        </div>
      </div>
      <div className="chat-composer-meta">
        {!id && apps.isSuccess && <p className="chat-apps">{apps.data.length} installed {apps.data.length === 1 ? 'app' : 'apps'}{apps.data.length > 0 && <> · {apps.data.map((app) => appLabel(app.name)).join(', ')}</>}</p>}
        <p id="chat-send-help">Enter to send · Shift + Enter for a new line</p>
      </div>
    </form>
  </section>
}

// A running run names its step: the phase it pushed, else the agent call that runs.
function runLine(run: ActiveRun, timezone: string): string {
  const name = run.subjectKey ? `${run.label} ${run.subjectKey}` : run.label
  if (run.state === 'scheduled') return `${name} is queued`
  if (run.state === 'parked') return `${name} waits for an answer`
  const step = phaseLine(run.phase, run.phaseEndsAt, timezone) ?? run.agentLabel
  return step ? `${name}: ${step}` : `${name} is running`
}

function Reply({ rows }: { rows: ReplyRow[] }) {
  return rows.map((row, index) => {
    if (row.type === 'text') return <Markdown key={index} source={row.text} />
    if (row.type === 'tool') return <ToolRow key={row.call.toolCallId} call={row.call} />
    return row.entries.length > 0 && <ol className="chat-plan" key="plan" aria-label="Agent plan">
      {row.entries.map((entry, entryIndex) => <li key={entryIndex} data-state={entry.status}>
        <span>{entry.content}</span><span className="chat-plan-status">{entry.status.replaceAll('_', ' ')}</span>
      </li>)}
    </ol>
  })
}

function ReplyBlocks({ blocks }: { blocks: ReplyBlock[] }) {
  return blocks.length > 0 && <div className="chat-blocks">
    <HasTextFormContext.Provider value={true}>
      {blocks.map(({ block, textForm }, index) => <AppSurface key={index} fallback={() => <Markdown source={textForm} />}>
        <Blocks blocks={[block]} />
      </AppSurface>)}
    </HasTextFormContext.Provider>
  </div>
}

function ReplyQuestion({ question, answer, sending, onAnswer }: {
  question: Question
  answer?: Message
  sending: boolean
  onAnswer: (body: string) => void
}) {
  return <div className="chat-question">
    <Markdown source={question.question} />
    {answer
      ? <div className="chat-question-answer">
        <div className="druks-chat-message__meta">Your answer</div>
        <div className="chat-user-body">{answer.body}</div>
      </div>
      : question.fields.length > 0
        ? <QuestionForm fields={question.fields} sending={sending} onAnswer={onAnswer} />
        : <div className="chat-question-choices">
          {question.choices.map((choice, index) => <Button key={index} size="compact" disabled={sending} onClick={() => onAnswer(choice)}>{choice}</Button>)}
        </div>}
  </div>
}

function QuestionForm({ fields, sending, onAnswer }: {
  fields: Field[]
  sending: boolean
  onAnswer: (body: string) => void
}) {
  const fieldState = useFieldState(fields)
  const [errors, setErrors] = useState<Record<string, string>>({})
  return <form className="chat-question-form" onSubmit={(event) => {
    event.preventDefault()
    const fieldErrors = formErrors(fields, fieldState.values)
    setErrors(fieldErrors)
    if (Object.keys(fieldErrors).length === 0) onAnswer(formAnswer(fields, fieldState.values))
  }}>
    <Fields fields={fields} values={fieldState.values} errors={errors} resets={fieldState.resets} onChange={fieldState.change} />
    <Button type="submit" variant="primary" size="compact" disabled={sending}>Submit</Button>
  </form>
}

function ToolRow({ call }: { call: ToolCall }) {
  const status = call.status ?? 'pending'
  const labels = { pending: 'Pending', in_progress: 'Running', completed: 'Complete', failed: 'Failed' }
  const output = 'rawOutput' in call ? call.rawOutput : call.content
  return <details className="druks-disclosure druks-panel chat-tool">
    <summary>
      <ChevronRight size={16} className="druks-disclosure__indicator chat-tool-chevron" aria-hidden="true" />
      <span className="chat-tool-label">{call.title}</span>
      <span className={`druks-badge druks-badge--tinted${status === 'completed' ? ' druks-badge--success' : status === 'failed' ? ' druks-badge--danger' : ''} chat-tool-status is-${status}`}>{status === 'completed' && <Check size={15} aria-hidden="true" />}{labels[status]}</span>
    </summary>
    <div className="druks-disclosure__content chat-tool-details">
      <h3>Input</h3>
      {call.rawInput === undefined ? <p>No input.</p> : <pre>{JSON.stringify(call.rawInput, null, 2)}</pre>}
      <h3>Output</h3>
      {output === undefined ? <p>{status === 'completed' || status === 'failed' ? 'No output.' : 'Waiting for output.'}</p> : <pre>{JSON.stringify(output, null, 2)}</pre>}
    </div>
  </details>
}
