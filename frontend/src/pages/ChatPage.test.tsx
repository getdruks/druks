import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Router } from 'wouter'

import { chatApi } from '../chat/api'
import { api } from '../api/client'
import type { Account, Agent, App, Block, Field } from '../api/types'
import type { Conversation, ConversationAction, Message, Question, RunStatus } from '../chat/state'
import { UserPreferencesProvider } from '../lib/preferences'
import { ChatPage } from './ChatPage'

vi.mock('../chat/api', () => ({ chatApi: { list: vi.fn(), get: vi.fn(), listActiveRuns: vi.fn(), listStatuses: vi.fn(), create: vi.fn(), send: vi.fn(), retry: vi.fn(), stop: vi.fn(), setPinned: vi.fn() } }))

class Socket {
  static instances: Socket[] = []
  onopen: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  close = vi.fn()
  url: string
  constructor(url: string) { this.url = url; Socket.instances.push(this) }
  receive(action: ConversationAction) { act(() => this.onmessage?.({ data: JSON.stringify(action) })) }
}

const message: Message = { id: '10', role: 'user', body: 'Check the active runs', blocks: [], state: 'delivered', replyTo: null, toolCalls: [], question: null, isInternal: false, file: null, transcript: '', createdAt: '2026-09-18T10:24:00Z', deliveredAt: '2026-09-18T10:24:01Z' }
const conversation: Conversation = {
  id: '01995a3c-0000-7000-8000-000000000001', title: message.body, source: 'web', userId: null, userName: '', createdAt: message.createdAt,
  messageCount: 1, activeMessageId: '10', messages: [message],
  isPinned: false, lastMessageAt: message.createdAt, agentId: null, agentName: null,
}
const admin: Account = { id: 'admin', username: 'admin@example.com', isDefault: true, role: 'admin' }
const reviewer: Agent = {
  id: 'agent-1', name: 'Reviewer', description: 'Reviews pull requests', instructions: 'Be brief.',
  skills: [], harness: 'claude', model: 'claude-opus', isPrivate: true, ownerId: 'admin', channelId: null, picture: null,
}

function mount(path = `/chat/${conversation.id}`, timezone?: string, account = admin) {
  window.history.replaceState(null, '', path)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const page = <Router>
    <Route path="/chat/:id?">{(params) => <ChatPage id={params.id} account={account} />}</Route>
  </Router>
  if (timezone) vi.spyOn(api, 'getPersonalSettings').mockResolvedValue({ timezone, theme: 'system', notificationChannelId: null, isTagRequired: false })
  render(<StrictMode><QueryClientProvider client={client}>
    {timezone ? <UserPreferencesProvider accountId="operator">{page}</UserPreferencesProvider> : page}
  </QueryClientProvider></StrictMode>)
  return client
}

async function connected() {
  await waitFor(() => expect(Socket.instances.length).toBeGreaterThan(0))
  const socket = Socket.instances.at(-1)!
  act(() => socket.onopen?.())
  return socket
}

beforeEach(() => {
  Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(chatApi.list).mockResolvedValue([conversation])
  vi.mocked(chatApi.get).mockResolvedValue(conversation)
  vi.mocked(chatApi.listActiveRuns).mockResolvedValue([])
  vi.mocked(chatApi.listStatuses).mockResolvedValue([])
  vi.mocked(chatApi.send).mockResolvedValue({ ...message, id: '11', body: 'Then check failures', state: 'pending' })
  vi.mocked(chatApi.retry).mockResolvedValue({ ...message, id: '11', state: 'pending' })
  vi.mocked(chatApi.create).mockResolvedValue(conversation)
  vi.mocked(chatApi.stop).mockResolvedValue(undefined)
  vi.mocked(chatApi.setPinned).mockImplementation(async (_id, isPinned) => ({ ...conversation, isPinned }))
  vi.spyOn(api, 'listApps').mockResolvedValue([])
  vi.spyOn(api, 'agents').mockResolvedValue([])
  vi.spyOn(api, 'services').mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Chat page', () => {
  it('offers Chat Settings to an admin only', () => {
    mount('/chat')
    expect(screen.getByRole('link', { name: 'Chat Settings' }).getAttribute('href')).toBe('/apps/chat/settings')

    cleanup()
    mount('/chat', undefined, { ...admin, id: 'member', role: 'member' })
    expect(screen.getByRole('heading', { name: 'Chat', level: 1 })).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Chat Settings' })).toBeNull()
  })

  it('distinguishes your messages, the agent, and Druks', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '11', role: 'assistant', body: 'The runs are ready.', state: null, replyTo: message.id },
      { ...message, id: '12', body: 'Send the reply to WhatsApp.', isInternal: true },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(within(turns[0]!).getByText('You')).toBeTruthy()
    expect(within(turns[0]!).getByText('Agent')).toBeTruthy()
    expect(within(turns[1]!).getByText('Druks')).toBeTruthy()
  })

  it('shows what each active run of the conversation does now', async () => {
    const run = { id: '01995a3c-0000-7000-8000-000000000002', label: 'Summarize', state: 'running' as const, subjectKey: 'note 7', agentLabel: 'Identify', phase: 'agent_running', phaseEndsAt: null }
    vi.mocked(chatApi.listActiveRuns).mockResolvedValue([run])
    const client = mount()
    expect(await screen.findByText('Summarize note 7: Identify')).toBeTruthy()
    vi.mocked(chatApi.listActiveRuns).mockResolvedValue([{ ...run, state: 'parked', agentLabel: null }])
    void client.refetchQueries({ queryKey: ['chat', 'active-runs'] })
    expect(await screen.findByText('Summarize note 7 waits for an answer')).toBeTruthy()
    vi.mocked(chatApi.listActiveRuns).mockResolvedValue([])
    void client.refetchQueries({ queryKey: ['chat', 'active-runs'] })
    await waitFor(() => expect(screen.queryByText(/^Summarize /)).toBeNull())
  })

  it.each([
    ['2026-09-19T10:24:00Z', '10:24'],
    ['2026-09-18T10:24:00Z', 'Sep 18 10:24'],
    ['2025-10-26T10:24:00Z', '2025-10-26'],
  ])('shows the date for %s in pinned rows, other rows, and the transcript', async (createdAt, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-19T12:00:00Z'))
    const saved = { ...conversation, isPinned: true, lastMessageAt: createdAt, messages: [
      { ...message, createdAt, state: 'replied' as const },
      { ...message, createdAt, id: '11', role: 'assistant' as const, body: 'Ready.', state: null, replyTo: message.id },
    ] }
    vi.mocked(chatApi.get).mockResolvedValue(saved)
    vi.mocked(chatApi.list).mockResolvedValue([saved, { ...conversation, id: '01995a3c-0000-7000-8000-000000000002', lastMessageAt: createdAt }])
    mount(`/chat/${conversation.id}`, 'UTC')
    await connected()
    await waitFor(() => {
      const times = screen.getAllByRole('time')
      expect(times).toHaveLength(5)
      times.forEach((time) => expect(time.textContent).toBe(expected))
    })
  })

  it('filters pinned and unpinned titles without replacing the thread or draft', async () => {
    const pinned = { ...conversation, id: '01995a3c-0000-7000-8000-000000000003', title: 'DEPLOY notes', isPinned: true }
    vi.mocked(chatApi.list).mockResolvedValue([conversation, pinned])
    mount()
    await connected()
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'Keep this draft' } })
    const search = screen.getByRole('searchbox', { name: 'Search conversations' })
    fireEvent.change(search, { target: { value: ' deploy ' } })
    const list = within(screen.getByRole('complementary', { name: 'Conversations' }))
    expect(list.getByRole('link', { name: /DEPLOY notes/ })).toBeTruthy()
    expect(list.queryByRole('link', { name: /Check the active runs/ })).toBeNull()
    expect(screen.getByRole('heading', { name: conversation.title! })).toBeTruthy()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep this draft')
    fireEvent.change(search, { target: { value: 'no title matches' } })
    expect(screen.getByText('No matching conversations.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(list.getByRole('link', { name: /DEPLOY notes/ })).toBeTruthy()
    expect(list.getByRole('link', { name: /Check the active runs/ })).toBeTruthy()
    expect(chatApi.create).not.toHaveBeenCalled()
    expect(chatApi.send).not.toHaveBeenCalled()
  })

  it('pins from the header, survives a live snapshot, and unpins from the list', async () => {
    vi.mocked(chatApi.setPinned).mockImplementation(async (_id, isPinned) => {
      const summary = { ...conversation, isPinned }
      vi.mocked(chatApi.list).mockResolvedValue([summary])
      return summary
    })
    const client = mount()
    const socket = await connected()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep the reply draft' } })
    const thread = within(screen.getByRole('region', { name: 'Conversation' }))
    fireEvent.click(thread.getByRole('button', { name: `Pin ${conversation.title}` }))
    await waitFor(() => expect(thread.getByRole('button', { name: `Pin ${conversation.title}` }).getAttribute('aria-pressed')).toBe('true'))
    socket.receive({ type: 'snapshot', ...conversation, isPinned: true, lastMessageAt: '2026-09-18T11:00:00Z' })
    expect(client.getQueryData<Conversation[]>(['chat', 'conversations'])?.[0]?.isPinned).toBe(true)
    expect(screen.getByRole('heading', { name: 'Pinned', level: 2 })).toBeTruthy()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep the reply draft')
    const list = within(screen.getByRole('complementary', { name: 'Conversations' }))
    fireEvent.click(list.getByRole('button', { name: `Pin ${conversation.title}` }))
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Pinned', level: 2 })).toBeNull())
    expect(chatApi.setPinned).toHaveBeenNthCalledWith(1, conversation.id, true)
    expect(chatApi.setPinned).toHaveBeenNthCalledWith(2, conversation.id, false)
    expect(window.location.pathname).toBe(`/chat/${conversation.id}`)
  })

  it('keeps a failed pin unchanged and retries the same action', async () => {
    vi.mocked(chatApi.setPinned).mockRejectedValueOnce(new Error('Unavailable'))
    mount()
    await connected()
    fireEvent.click(screen.getAllByRole('button', { name: `Pin ${conversation.title}` })[0]!)
    expect((await screen.findByRole('alert')).textContent).toContain('Could not save the pin.')
    expect(screen.getAllByRole('button', { name: `Pin ${conversation.title}`, pressed: false })).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(chatApi.setPinned).toHaveBeenCalledTimes(2))
    expect(chatApi.setPinned).toHaveBeenLastCalledWith(conversation.id, true)
  })

  it('groups by the account calendar across daylight saving changes', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-25T23:30:00Z'))
    vi.mocked(chatApi.list).mockResolvedValue([
      { ...conversation, title: 'Today in Madrid', lastMessageAt: '2026-10-25T23:10:00Z' },
      { ...conversation, id: '01995a3c-0000-7000-8000-000000000002', title: 'Yesterday in Madrid', lastMessageAt: '2026-10-24T22:30:00Z' },
      { ...conversation, id: '01995a3c-0000-7000-8000-000000000003', title: 'Earlier in Madrid', lastMessageAt: '2026-10-24T21:30:00Z' },
    ])
    mount('/chat/new', 'Europe/Madrid')
    await waitFor(() => expect(screen.getByRole('region', { name: 'Today' }).textContent).toContain('Today in Madrid'))
    expect(screen.getByRole('region', { name: 'Yesterday' }).textContent).toContain('Yesterday in Madrid')
    expect(screen.getByRole('region', { name: 'Earlier' }).textContent).toContain('Earlier in Madrid')
  })

  it('moves an older conversation to Today when a new message arrives', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-19T12:00:00Z'))
    mount()
    const socket = await connected()
    expect(screen.getByRole('region', { name: 'Yesterday' })).toBeTruthy()
    socket.receive({ type: 'snapshot', ...conversation, lastMessageAt: '2026-09-19T11:59:00Z' })
    expect(await screen.findByRole('region', { name: 'Today' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Yesterday' })).toBeNull()
  })

  it('offers starter prompts without sending or creating a conversation', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([])
    mount('/chat/new')
    expect(await screen.findByText('Your conversations will appear here.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'What needs my attention?' }))
    const composer = screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement
    expect(composer.value).toBe('What needs my attention?')
    expect(document.activeElement).toBe(composer)
    expect(chatApi.create).not.toHaveBeenCalled()
    expect(chatApi.send).not.toHaveBeenCalled()
    expect(screen.queryByText(/last reply/)).toBeNull()
  })

  it('finds an unnamed WhatsApp conversation by its displayed name', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([{ ...conversation, title: null, source: 'whatsapp', userName: 'Alex', isPinned: true }])
    mount('/chat/new')
    expect(await screen.findByRole('link', { name: /Alex/ })).toBeTruthy()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'alex' } })
    expect(screen.getByRole('link', { name: /Alex/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Pin Alex' })).toBeTruthy()
  })

  it('moves Today to Yesterday when the account calendar passes midnight', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-18T21:59:50Z'))
    mount('/chat/new', 'Europe/Madrid')
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(screen.getByRole('region', { name: 'Today' })).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(screen.getByRole('region', { name: 'Yesterday' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Today' })).toBeNull()
  })

  it('keeps conversations in date order after a live snapshot', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([
      conversation,
      { ...conversation, id: '01995a3c-0000-7000-8000-000000000002', title: 'Previous day', createdAt: '2026-09-17T16:00:00Z', lastMessageAt: '2026-09-17T16:00:00Z' },
    ])
    mount()
    const socket = await connected()
    socket.receive({ type: 'snapshot', ...conversation })
    const links = within(screen.getByRole('complementary', { name: 'Conversations' })).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/chat/new', `/chat/${conversation.id}`, '/chat/01995a3c-0000-7000-8000-000000000002'])
  })

  it('shows an unnamed conversation as New conversation', async () => {
    const unnamed = { ...conversation, title: null }
    vi.mocked(chatApi.list).mockResolvedValue([unnamed])
    vi.mocked(chatApi.get).mockResolvedValue(unnamed)
    mount()
    await connected()
    const list = within(screen.getByRole('complementary', { name: 'Conversations' }))
    expect(list.getAllByRole('link').find((link) => link.getAttribute('href') === `/chat/${conversation.id}`)?.textContent).toContain('New conversation')
    expect(screen.getByRole('heading', { name: 'New conversation' })).toBeTruthy()
  })

  it('creates a conversation only from its first message', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([])
    mount('/chat/new')
    expect(chatApi.create).not.toHaveBeenCalled()
    expect(screen.queryByText('No messages yet')).toBeNull()
    await waitFor(() => expect(api.agents).toHaveResolved())
    await screen.findByText('0 installed apps')
    expect(screen.queryByRole('combobox', { name: 'Talk to' })).toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: message.body } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(chatApi.create).toHaveBeenCalledExactlyOnceWith(message.body, undefined))
    await connected()
    expect(window.location.pathname).toBe(`/chat/${conversation.id}`)
    expect(screen.getByRole('heading', { name: message.body })).toBeTruthy()
    expect(screen.queryByText('Private conversation')).toBeNull()
  })

  it('starts a conversation with an agent the person can talk to', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([])
    vi.spyOn(api, 'agents').mockResolvedValue([
      reviewer,
      { ...reviewer, id: 'agent-2', name: 'Writer', isPrivate: false, ownerId: 'member' },
      { ...reviewer, id: 'agent-3', name: 'Diary', ownerId: 'member' },
    ])
    mount('/chat/new')
    const picker = await screen.findByRole('combobox', { name: 'Talk to' })
    expect(within(picker).getAllByRole('option').map((option) => option.textContent)).toEqual(['Druks', 'Reviewer', 'Writer'])
    fireEvent.change(picker, { target: { value: reviewer.id } })
    expect(screen.getByText(reviewer.description)).toBeTruthy()
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: message.body } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(chatApi.create).toHaveBeenCalledExactlyOnceWith(message.body, reviewer.id))
  })

  it('names the agent of a conversation in the list and the header', async () => {
    const withAgent = { ...conversation, activeMessageId: null, agentId: reviewer.id, agentName: reviewer.name }
    const replying = { ...withAgent, id: '01995a3c-0000-7000-8000-000000000002', title: 'Review the gate', activeMessageId: '20' }
    vi.mocked(chatApi.list).mockResolvedValue([withAgent, replying])
    mount()
    const socket = await connected()
    socket.receive({ type: 'snapshot', ...withAgent })
    const row = await screen.findByRole('link', { name: /Check the active runs/ })
    expect(row.textContent).toContain('1 message / Reviewer')
    expect((await screen.findByRole('link', { name: /Review the gate/ })).textContent).toContain('Reviewer is replying')
    expect(within(screen.getByRole('region', { name: 'Conversation' })).getByText(/1 message/).textContent).toContain('/ Reviewer')
  })

  it('saves another message while the agent replies and keeps Send beside Stop', async () => {
    mount()
    await connected()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Then check failures' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(await screen.findByText('Queued')).toBeTruthy()
    expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, 'Then check failures')
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, message.id))
    expect(screen.getByText('Agent is replying')).toBeTruthy()
  })

  it('names a batched WhatsApp turn by its newest delivered message', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, source: 'whatsapp', messages: [
      message,
      { ...message, id: '11', body: 'And the failures' },
      { ...message, id: '12', body: 'Later question', state: 'pending', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(within(turns[0]!).queryByRole('status')).toBeNull()
    expect(within(turns[1]!).getByRole('status').textContent).toBe('Agent is replying…')
    expect(within(turns[2]!).getByText('Queued')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, '11'))
  })

  it('shows connection progress at the next reply without infrastructure copy', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [{ ...message, state: 'pending' }] })
    mount()
    await connected()
    const status = screen.getByText('Connecting…')
    expect(status.closest('.chat-reply')).toBeTruthy()
    expect(screen.queryByText(/get the sandbox|start the agent|No messages yet/i)).toBeNull()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, message.id))
  })

  it('streams plain agent text and expandable tool input and output without row times', async () => {
    mount()
    const socket = await connected()
    const emit = (sequence: number, update: Extract<ConversationAction, { type: 'event' }>['notification']['update']) => socket.receive({ type: 'event', messageId: '10', epoch: 'sandbox', sequence, notification: { sessionId: 'agent', update } })
    emit(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'I will check the gates.' } })
    emit(2, { sessionUpdate: 'tool_call', toolCallId: 'gate', title: 'Read the approval gate', rawInput: { run: 'example' }, status: 'in_progress' })
    emit(3, { sessionUpdate: 'tool_call_update', toolCallId: 'gate', status: 'completed', rawOutput: { gate: 'approval' } })
    emit(4, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'One run needs approval.' } })
    expect(screen.getByText('I will check the gates.')).toBeTruthy()
    expect(screen.getByText('One run needs approval.')).toBeTruthy()
    const tool = screen.getByText('Read the approval gate').closest('details')!
    fireEvent.click(tool.querySelector('summary')!)
    expect(tool.hasAttribute('open')).toBe(true)
    expect(within(tool).getByText(/"run": "example"/)).toBeTruthy()
    expect(within(tool).getByText(/"gate": "approval"/)).toBeTruthy()
    expect(within(tool).queryByRole('time')).toBeNull()
  })

  it.each(['interrupted', 'cancelled', 'failed'] as const)('resends a %s turn only after Send again and preserves the current draft', async (state) => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [{ ...message, state, deliveredAt: null }] })
    mount()
    await connected()
    expect(screen.getByText(state === 'failed' ? 'Failed' : state === 'cancelled' ? 'Cancelled' : 'Interrupted')).toBeTruthy()
    expect(chatApi.send).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep this draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send again' }))
    await waitFor(() => expect(chatApi.retry).toHaveBeenCalledExactlyOnceWith(conversation.id, message.id))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep this draft')
  })

  it('reconnects the stream without resending a saved message or duplicating text', async () => {
    mount()
    const first = await connected()
    const chunk: ConversationAction = { type: 'event', messageId: '10', epoch: 'sandbox', sequence: 1, notification: { sessionId: 'agent', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'One reply.' } } } }
    first.receive(chunk)
    vi.useFakeTimers()
    act(() => first.onclose?.({ code: 1006 }))
    expect(screen.getByText('Reconnecting…')).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    const second = Socket.instances.at(-1)!
    expect(second).not.toBe(first)
    act(() => second.onopen?.())
    second.receive(chunk)
    expect(screen.getAllByText('One reply.')).toHaveLength(1)
    expect(chatApi.send).not.toHaveBeenCalled()
  })

  it('shows a reminder, which answers no message, in time order', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '11', role: 'assistant', body: 'Reminder set for 09:21.', state: null, replyTo: '10' },
      { ...message, id: '12', role: 'assistant', body: 'Take out the trash!', state: null, replyTo: null },
      { ...message, id: '13', body: 'Done', state: 'replied' },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(turns.map((turn) => turn.getAttribute('aria-label'))).toEqual(['Message and reply', 'Agent message', 'Message and reply'])
    expect(within(turns[1]!).getByText('Agent')).toBeTruthy()
    expect(within(turns[1]!).getByText('Take out the trash!')).toBeTruthy()
  })

  it('pairs each reply with its user message by replyTo when replies arrive out of order', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '11', body: 'Next request', state: 'replied' },
      { ...message, id: '13', role: 'assistant', body: 'Second answer', state: null, replyTo: '11', deliveredAt: null },
      { ...message, id: '12', role: 'assistant', body: 'First answer', state: null, replyTo: '10', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(turns[0]?.textContent).toContain('First answer')
    expect(turns[0]?.textContent).not.toContain('Second answer')
    expect(turns[1]?.textContent).toContain('Next request')
    expect(turns[1]?.textContent).toContain('Second answer')
  })

  it('keeps the draft after a failed send and ignores Enter during IME composition', async () => {
    mount()
    await connected()
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'Keep my message' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(chatApi.send).not.toHaveBeenCalled()
    vi.mocked(chatApi.send).mockRejectedValueOnce(new Error('Message was not saved.'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect((input as HTMLTextAreaElement).value).toBe('Keep my message')
  })

  it('keeps a cancelled partial reply with its turn and skips cancelled pending messages', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'cancelled' },
      { ...message, id: '11', body: 'Do not send this', state: 'cancelled', deliveredAt: null },
      { ...message, id: '12', body: 'Continue', state: 'replied' },
      { ...message, id: '13', role: 'assistant', body: 'Partial answer', state: null, replyTo: '10', deliveredAt: null },
      { ...message, id: '14', role: 'assistant', body: 'Next answer', state: null, replyTo: '12', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(turns[0]?.textContent).toContain('Partial answer')
    expect(turns[1]?.textContent).not.toContain('answer')
    expect(turns[2]?.textContent).toContain('Next answer')
    expect(screen.getAllByRole('button', { name: 'Send again' })).toHaveLength(2)
    expect(chatApi.send).not.toHaveBeenCalled()
  })
})

describe('Chat questions', () => {
  const choices: Question = { question: 'Which database?', choices: ['Postgres', 'MySQL'], fields: [] }
  const title: Field = { field: 'text', name: 'title', label: 'Title', helpText: '', isRequired: true, value: '', placeholder: '' }
  const where: Field = { field: 'select', name: 'where', label: 'Where', helpText: '', isRequired: false, options: [{ value: 'staging', label: 'Staging' }, { value: 'production', label: 'Production' }], value: 'staging' }

  // The turn of the person's message, whose reply asks.
  function asked(reply: Partial<Message>): Conversation {
    return { ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '20', role: 'assistant', body: 'I read both.', state: null, replyTo: message.id, deliveredAt: null, question: choices, ...reply },
    ] }
  }

  function answer(body: string): Message {
    return { ...message, id: '21', body, state: 'pending', deliveredAt: null, createdAt: '2026-09-18T10:25:00Z' }
  }

  function form(...fields: Field[]): Partial<Message> {
    return { question: { question: 'Where does it ship?', choices: [], fields } }
  }

  it('shows the question above a button for each choice, and sends the chosen label as a message', async () => {
    vi.mocked(chatApi.get).mockResolvedValue(asked({}))
    vi.mocked(chatApi.send).mockResolvedValue(answer('Postgres'))
    mount()
    await connected()
    const turn = within(screen.getAllByRole('article')[0]!)
    const postgres = turn.getByRole('button', { name: 'Postgres' })
    expect(turn.getByText('Which database?').compareDocumentPosition(postgres) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(turn.getByRole('button', { name: 'MySQL' })).toBeTruthy()
    fireEvent.click(postgres)
    await waitFor(() => expect(turn.queryByRole('button', { name: 'Postgres' })).toBeNull())
    expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, 'Postgres')
    expect(turn.getByText('Which database?')).toBeTruthy()
    expect(turn.getByText('Your answer')).toBeTruthy()
    expect(turn.getByText('Postgres')).toBeTruthy()
  })

  it('submits a form of three fields as a message with a line for each field', async () => {
    const notify: Field = { field: 'checkbox', name: 'notify', label: 'Notify the owner', helpText: '', isRequired: false, value: false }
    const body = 'Title: Fix the gate\nWhere: Production\nNotify the owner: No'
    vi.mocked(chatApi.get).mockResolvedValue(asked(form(title, where, notify)))
    vi.mocked(chatApi.send).mockResolvedValue(answer(body))
    mount()
    await connected()
    const turn = within(screen.getAllByRole('article')[0]!)
    fireEvent.change(turn.getByRole('textbox', { name: /Title/ }), { target: { value: 'Fix the gate' } })
    fireEvent.change(turn.getByRole('combobox', { name: 'Where' }), { target: { value: 'production' } })
    fireEvent.click(turn.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(turn.queryByRole('button', { name: 'Submit' })).toBeNull())
    expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, body)
    expect(turn.queryByRole('textbox')).toBeNull()
    expect(turn.getByText('Where does it ship?')).toBeTruthy()
    expect(turn.getByText(/Title: Fix the gate/)).toBeTruthy()
  })

  it.each([
    ['no text', { body: '', toolCalls: [] }],
    ['only notes', { body: 'Let me check.', toolCalls: [{ toolCallId: 'ask', title: 'Ask', textOffset: 13 }] }],
  ])('shows the Agent label and the question of a reply with %s', async (_reply, reply) => {
    vi.mocked(chatApi.get).mockResolvedValue(asked(reply))
    mount()
    await connected()
    const turn = within(screen.getAllByRole('article')[0]!)
    expect(turn.getByText('Agent')).toBeTruthy()
    expect(turn.getByText('Which database?')).toBeTruthy()
    expect(turn.getByRole('button', { name: 'Postgres' })).toBeTruthy()
  })

  it('starts each field at its declared value and drops the edits when the fields change', async () => {
    const budget: Field = { field: 'number', name: 'budget', label: 'Budget', helpText: '', isRequired: false, value: 10, minimum: null, maximum: null, step: null }
    const tags: Field = { field: 'multi_select', name: 'tags', label: 'Tags', helpText: '', isRequired: false, options: [{ value: 'rack', label: 'Rack' }, { value: 'shelf', label: 'Shelf' }], value: ['rack'] }
    const size: Field = { field: 'radio', name: 'size', label: 'Size', helpText: '', isRequired: true, options: [{ value: 's', label: 'Small' }, { value: 'l', label: 'Large' }], value: '' }
    const region: Field = { ...where, name: 'region', label: 'Region', isRequired: true, value: '' }
    vi.mocked(chatApi.get).mockResolvedValue(asked(form(budget, region, tags, size)))
    mount()
    const socket = await connected()
    expect((screen.getByRole('spinbutton', { name: 'Budget' }) as HTMLInputElement).value).toBe('10')
    expect((screen.getByRole('combobox', { name: /Region/ }) as HTMLSelectElement).value).toBe('staging')
    expect((screen.getByRole('checkbox', { name: 'Rack' }) as HTMLInputElement).checked).toBe(true)
    expect((screen.getByRole('radio', { name: 'Small' }) as HTMLInputElement).checked).toBe(true)
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Budget' }), { target: { value: '20' } })
    socket.receive({ type: 'snapshot', ...asked(form(budget, region, tags, size)) })
    expect((screen.getByRole('spinbutton', { name: 'Budget' }) as HTMLInputElement).value).toBe('20')
    socket.receive({ type: 'snapshot', ...asked(form({ ...budget, name: 'cost', label: 'Cost' }, region, tags, size)) })
    expect((screen.getByRole('spinbutton', { name: 'Cost' }) as HTMLInputElement).value).toBe('10')
  })

  it('shows an optional select with no default as blank, and sends (no answer) for it', async () => {
    const staging: Field = { field: 'select', name: 'where', label: 'Where', helpText: '', isRequired: false, options: [{ value: 'staging', label: 'Staging' }], value: '' }
    vi.mocked(chatApi.get).mockResolvedValue(asked(form(staging)))
    vi.mocked(chatApi.send).mockResolvedValue(answer('Where: (no answer)'))
    mount()
    await connected()
    expect((screen.getByRole('combobox', { name: 'Where' }) as HTMLSelectElement).value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, 'Where: (no answer)'))
  })

  it('sends nothing while a required field has no answer, and takes 0', async () => {
    const count: Field = { field: 'number', name: 'count', label: 'Count', helpText: '', isRequired: true, value: 0, minimum: null, maximum: null, step: null }
    const size: Field = { field: 'radio', name: 'size', label: 'Size', helpText: '', isRequired: true, options: [{ value: 's', label: 'Small' }, { value: 'l', label: 'Large' }], value: '' }
    const tags: Field = { field: 'multi_select', name: 'tags', label: 'Tags', helpText: '', isRequired: true, options: [{ value: 'rack', label: 'Rack' }, { value: 'shelf', label: 'Shelf' }], value: [] }
    const body = 'Title: Fix the gate\nCount: 0\nSize: Small\nTags: Shelf'
    vi.mocked(chatApi.get).mockResolvedValue(asked(form(title, count, size, tags)))
    vi.mocked(chatApi.send).mockResolvedValue(answer(body))
    mount()
    await connected()
    fireEvent.change(screen.getByRole('textbox', { name: /Title/ }), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    expect(await screen.findByText('Answer "Title".')).toBeTruthy()
    expect(screen.getByText('Answer "Tags".')).toBeTruthy()
    expect(screen.queryByText('Answer "Count".')).toBeNull()
    expect(screen.queryByText('Answer "Size".')).toBeNull()
    expect(chatApi.send).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox', { name: /Title/ }), { target: { value: 'Fix the gate' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'Shelf' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, body))
  })

  it('sends once while it waits, and keeps the controls and the answers after a failed send', async () => {
    let fail: (error: Error) => void = () => {}
    vi.mocked(chatApi.get).mockResolvedValue(asked(form(title, where)))
    vi.mocked(chatApi.send).mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))
    mount()
    await connected()
    fireEvent.change(screen.getByRole('textbox', { name: /Title/ }), { target: { value: 'Fix the gate' } })
    const submit = screen.getByRole('button', { name: 'Submit' }) as HTMLButtonElement
    fireEvent.click(submit)
    await waitFor(() => expect(submit.disabled).toBe(true))
    fireEvent.click(submit)
    expect(chatApi.send).toHaveBeenCalledOnce()
    await act(async () => fail(new Error('Message was not saved.')))
    expect((await screen.findByRole('alert')).textContent).toContain('Message was not saved.')
    expect(submit.disabled).toBe(false)
    expect((screen.getByRole('textbox', { name: /Title/ }) as HTMLInputElement).value).toBe('Fix the gate')
  })
})

describe('Run statuses', () => {
  const reply: Message = { ...message, id: '11', role: 'assistant', body: 'I started the summary.', state: null, replyTo: '10', createdAt: '2026-09-18T10:24:40Z' }
  const next: Message = { ...message, id: '12', body: 'Anything else?', state: 'replied', createdAt: '2026-09-18T10:30:00Z' }
  const thread: Conversation = { ...conversation, activeMessageId: null, messages: [{ ...message, state: 'replied' }, reply, next] }
  const reportedRun = { id: '01995a3c-0000-7000-8000-000000000002', label: 'Summarize', state: 'running' as const, subjectKey: 'note 7', agentLabel: 'Identify', phase: null, phaseEndsAt: null }
  const unreportedRun = { ...reportedRun, id: '01995a3c-0000-7000-8000-000000000003', label: 'Build', subjectKey: 'acme/api', state: 'parked' as const }

  function markdown(text: string): Block {
    return { block: 'markdown', text }
  }

  function runStatus(blocks: Block[], status: Partial<RunStatus> = {}): RunStatus {
    return { id: '01995a3c-0000-7000-8000-0000000000a1', run: reportedRun.id, app: 'field_notes', blocks, startedAt: '2026-09-18T10:24:30Z', ...status }
  }

  // Each status shows its text. Each turn shows its label.
  function shown() {
    return screen.getAllByRole('article').map((article) => article.getAttribute('aria-label') === 'Run status' ? article.textContent : article.getAttribute('aria-label'))
  }

  // React Query reports a read on a timer that the fake clock starts one millisecond later.
  async function advance(milliseconds: number) {
    await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds + 1) })
  }

  async function open() {
    mount()
    await advance(0)
    act(() => Socket.instances.at(-1)!.onopen?.())
  }

  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(chatApi.get).mockResolvedValue(thread)
  })

  it('places each status after every turn that started at or before its run, also when its first report comes late', async () => {
    const reminder: Message = { ...message, id: '13', role: 'assistant', body: 'Time to deploy.', state: null, replyTo: null, createdAt: '2026-09-18T10:26:00Z' }
    vi.mocked(chatApi.get).mockResolvedValue({ ...thread, messages: [{ ...message, state: 'replied' }, reply, reminder, next] })
    await open()
    expect(shown()).toEqual(['Message and reply', 'Agent message', 'Message and reply'])

    vi.mocked(chatApi.listStatuses).mockResolvedValue([
      runStatus([markdown('Release')], { id: '01995a3c-0000-7000-8000-0000000000a4', startedAt: next.createdAt }),
      runStatus([markdown('Build B')], { id: '01995a3c-0000-7000-8000-0000000000a2', startedAt: message.createdAt }),
      runStatus([markdown('Deploy')], { id: '01995a3c-0000-7000-8000-0000000000a3', startedAt: '2026-09-18T10:25:00Z' }),
      runStatus([markdown('Build A')], { id: '01995a3c-0000-7000-8000-0000000000a1', startedAt: message.createdAt }),
    ])
    await advance(2000)

    expect(shown()).toEqual(['Message and reply', 'Build A', 'Build B', 'Deploy', 'Agent message', 'Message and reply', 'Release'])
    expect(within(screen.getAllByRole('article')[0]!).getByText('I started the summary.')).toBeTruthy()
  })

  it('changes one status in place as the run reports and fails', async () => {
    const failed: Block = { block: 'callout', tone: 'danger', title: '', text: 'Failed.', controls: [] }
    vi.mocked(chatApi.listActiveRuns).mockResolvedValue([reportedRun, unreportedRun])
    vi.mocked(chatApi.listStatuses).mockResolvedValue([runStatus([markdown('Reading the note.')])])
    await open()
    expect(shown()).toEqual(['Message and reply', 'Reading the note.', 'Message and reply'])
    expect(screen.getByText('Build acme/api waits for an answer')).toBeTruthy()
    expect(screen.queryByText('Summarize note 7: Identify')).toBeNull()

    vi.mocked(chatApi.listStatuses).mockResolvedValue([runStatus([markdown('Writing the gist.')])])
    await advance(2000)
    expect(shown()).toEqual(['Message and reply', 'Writing the gist.', 'Message and reply'])

    vi.mocked(chatApi.listActiveRuns).mockResolvedValue([unreportedRun])
    vi.mocked(chatApi.listStatuses).mockResolvedValue([runStatus([markdown('Writing the gist.'), failed])])
    await advance(2000)
    expect(shown()).toEqual(['Message and reply', 'Writing the gist.Failed.', 'Message and reply'])
    expect(screen.getByText('Build acme/api waits for an answer')).toBeTruthy()
  })

  it('resolves a link of a status through the pages of its app, and leaves it unresolved with no app', async () => {
    const roster = [{ name: 'field_notes', icon: 'notebook', description: '', builtin: false, subjectTypes: ['note'], hasFrontend: false, navigation: [], operations: [], pages: [
      { name: 'note', label: 'Note', path: '/field_notes/notes/{note_id}', parent: '', subjectType: '', order: 0 },
    ] }] as App[]
    const link = (label: string): Block => ({ block: 'link', label, page: 'note', arguments: { note_id: '7' }, url: '', subject: null })
    vi.spyOn(api, 'listApps').mockResolvedValue(roster)
    vi.mocked(chatApi.listStatuses).mockResolvedValue([
      runStatus([link('Open the note')]),
      runStatus([link('Open the old note')], { id: '01995a3c-0000-7000-8000-0000000000a2', app: '' }),
    ])
    await open()

    expect(screen.getByRole('link', { name: 'Open the note' }).getAttribute('href')).toBe('/field_notes/notes/7')
    expect(screen.getByText('Open the old note').closest('a')).toBeNull()
    expect(screen.getByText('Open the old note').className).toContain('dui-link-broken')
  })
})

describe('Chat blocks', () => {
  const chart: Block = { block: 'chart', kind: 'bar', title: 'Builds this week', categories: ['Mon', 'Tue'], series: [{ label: 'Builds', points: [4, 7] }], categoryLabel: '', valueLabel: '' }
  const facts: Block = { block: 'facts', title: 'Project', facts: [{ label: 'Owner', value: { value: 'text', text: 'Ana', description: '', link: null } }] }
  const table: Block = {
    block: 'table', title: 'Runs', empty: null, select: '', actions: [],
    columns: [{ label: 'Run', align: 'start' }, { label: 'State', align: 'start' }],
    rows: [{ cells: [{ value: 'text', text: 'Build 7', description: '', link: null }, { value: 'status', label: 'failed', tone: 'danger', link: null }], detail: '', key: '' }],
  }
  const question: Question = { question: 'Which run first?', choices: ['Build 7', 'Build 8'], fields: [] }

  // The turn of the person's message, whose reply shows blocks.
  function shown(reply: Partial<Message>): Conversation {
    return { ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '20', role: 'assistant', body: 'Here is the week.', state: null, replyTo: message.id, deliveredAt: null, ...reply },
    ] }
  }

  it('draws a chart, facts, and a table as the dashboard does, after the text and before the question', async () => {
    vi.mocked(chatApi.get).mockResolvedValue(shown({ question, blocks: [chart, facts, table].map((block) => ({ block, textForm: 'Not drawn' })) }))
    mount()
    await connected()
    const turn = within(screen.getAllByRole('article')[0]!)
    const order = [
      turn.getByText('Here is the week.'),
      turn.getByRole('table', { name: 'Builds this week' }),
      turn.getByText('Owner'),
      turn.getByRole('table', { name: 'Runs' }),
      turn.getByText('Which run first?'),
    ]
    order.slice(1).forEach((after, index) => expect(order[index]!.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy())
    expect(turn.queryByText('Not drawn')).toBeNull()
  })

  it('shows a reply that has only blocks', async () => {
    vi.mocked(chatApi.get).mockResolvedValue(shown({ body: '', blocks: [{ block: facts, textForm: 'Not drawn' }] }))
    mount()
    await connected()
    expect(within(screen.getAllByRole('article')[0]!).getByText('Owner')).toBeTruthy()
  })

  it.each([
    ['an unknown block', { block: 'hologram' }],
    ['a block that fails to draw', { block: 'facts', title: 'Broken' }],
    ['an unknown block inside a stack', { block: 'stack', gap: 'medium', blocks: [{ block: 'hologram' }] }],
    ['an unknown value inside a block', { block: 'facts', title: 'Project', facts: [{ label: 'Budget', value: { value: 'money' } }] }],
  ])('shows the text form of %s in its place and keeps the rest of the conversation', async (_block, block) => {
    vi.mocked(chatApi.get).mockResolvedValue(shown({ question, blocks: [
      { block: block as unknown as Block, textForm: '**The text form**' }, { block: facts, textForm: 'Not drawn' },
    ] }))
    mount()
    await connected()
    const turn = within(screen.getAllByRole('article')[0]!)
    expect(turn.getByText('The text form')).toBeTruthy()
    expect(turn.getByText('Here is the week.')).toBeTruthy()
    expect(turn.getByText('Owner')).toBeTruthy()
    expect(turn.getByRole('button', { name: 'Build 7' })).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
