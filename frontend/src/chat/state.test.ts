import type { SessionUpdate } from '@agentclientprotocol/sdk'
import { describe, expect, it } from 'vitest'

import type { Field } from '../api/types'
import { compareConversations, conversationReducer, formAnswer, formErrors, initialConversation, questionAnswer, replyRows, savedReplyRows, type Conversation, type ConversationAction, type Message } from './state'

const conversation: Conversation = {
  id: '01995a3c-0000-7000-8000-000000000001', title: 'Read the gate', source: 'web', userId: null, userName: '', createdAt: '2026-09-18T10:00:00Z',
  messageCount: 1, activeMessageId: '2',
  isPinned: false, lastMessageAt: '2026-09-18T10:00:00Z',
  messages: [{ id: '2', role: 'user', body: 'Read the gate', state: 'delivered', replyTo: null, toolCalls: [], question: null, isInternal: false, file: null, transcript: '', createdAt: '2026-09-18T10:00:00Z', deliveredAt: '2026-09-18T10:00:01Z' }],
}

function event(sequence: number, update: SessionUpdate, epoch = 'one'): ConversationAction {
  return { type: 'event', sequence, epoch, messageId: '2', notification: { sessionId: 'agent', update } }
}

describe('Chat events', () => {
  it('orders conversations by their latest message with a stable id tie-breaker', () => {
    const later = { ...conversation, id: '01995a3c-0000-7000-8000-000000000002' }
    const latest = { ...conversation, id: '01995a3c-0000-7000-8000-000000000003', lastMessageAt: '2026-09-19T10:00:00Z' }
    const pinned = { ...conversation, isPinned: true }
    expect([pinned, latest, later].sort(compareConversations).map((item) => item.id)).toEqual([latest.id, later.id, pinned.id])
  })

  it('keeps text and tool calls in order and merges partial tool updates', () => {
    let state = conversationReducer(initialConversation, { type: 'snapshot', ...conversation })
    state = conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'I will read it.' } }))
    state = conversationReducer(state, event(2, { sessionUpdate: 'tool_call', toolCallId: 'gate', title: 'Read the gate', rawInput: { run: 'run-one' }, status: 'pending' }))
    state = conversationReducer(state, event(3, { sessionUpdate: 'tool_call_update', toolCallId: 'gate', status: 'completed', rawOutput: { gate: 'approval' } }))
    state = conversationReducer(state, event(4, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'It needs ' } }))
    state = conversationReducer(state, event(5, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'approval.' } }))
    expect(state.turns['2']?.rows).toEqual([
      { type: 'text', text: 'I will read it.' },
      { type: 'tool', call: expect.objectContaining({ title: 'Read the gate', rawInput: { run: 'run-one' }, status: 'completed', rawOutput: { gate: 'approval' } }) },
      { type: 'text', text: 'It needs approval.' },
    ])
  })

  it('replaces the complete plan and keeps null tool titles unchanged', () => {
    let rows = replyRows([], { sessionUpdate: 'plan', entries: [{ content: 'Read', priority: 'high', status: 'pending' }] })
    rows = replyRows(rows, { sessionUpdate: 'plan', entries: [{ content: 'Report', priority: 'low', status: 'completed' }] })
    expect(rows).toEqual([{ type: 'plan', entries: [{ content: 'Report', priority: 'low', status: 'completed' }] }])
    rows = replyRows(rows, { sessionUpdate: 'tool_call', toolCallId: 'one', title: 'Read', rawInput: {} })
    rows = replyRows(rows, { sessionUpdate: 'tool_call_update', toolCallId: 'one', title: null, rawOutput: null })
    expect(rows.at(-1)).toEqual({ type: 'tool', call: expect.objectContaining({ title: 'Read', rawInput: {}, rawOutput: null }) })
    rows = replyRows(rows, { sessionUpdate: 'plan', entries: [] })
    expect(rows[0]).toEqual({ type: 'plan', entries: [] })
  })

  it('does not duplicate replayed chunks and resets the cursor for a new sandbox', () => {
    const chunk = event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'Hello' } })
    let state = conversationReducer(initialConversation, { type: 'snapshot', ...conversation })
    state = conversationReducer(state, chunk)
    state = conversationReducer(state, { type: 'snapshot', ...conversation })
    expect(conversationReducer(state, chunk)).toBe(state)
    state = conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'New turn' } }, 'two'))
    expect(state.turns['2']?.rows).toEqual([{ type: 'text', text: 'New turn' }])
  })

  it.each(['replied', 'cancelled', 'failed'] as const)('uses stored %s replies, ignores old events, and clears an error on the next snapshot', (messageState) => {
    const completed: Conversation = { ...conversation, activeMessageId: null, messages: conversation.messages.map((message) => ({ ...message, state: messageState })) }
    const state = conversationReducer(initialConversation, { type: 'snapshot', ...completed })
    expect(conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'duplicate' } }))).toBe(state)
    const failed = conversationReducer(state, { type: 'error', detail: 'Unavailable' })
    expect(conversationReducer(failed, { type: 'snapshot', ...completed }).error).toBeNull()
  })

  it('restores tool positions after non-BMP text and multiple tools at the same position', () => {
    expect(savedReplyRows({
      id: '4', role: 'assistant', state: null, replyTo: '2', createdAt: conversation.createdAt, body: '🌱 Read.Done.', deliveredAt: null,
      question: null, isInternal: false, file: null, transcript: '',
      toolCalls: [
        { toolCallId: 'one', title: 'Read', textOffset: 7 },
        { toolCallId: 'two', title: 'Check', textOffset: 7 },
      ],
    })).toEqual([
      { type: 'text', text: '🌱 Read.' },
      { type: 'tool', call: { toolCallId: 'one', title: 'Read', textOffset: 7 } },
      { type: 'tool', call: { toolCallId: 'two', title: 'Check', textOffset: 7 } },
      { type: 'text', text: 'Done.' },
    ])
  })
})

describe('Chat questions', () => {
  const [person] = conversation.messages as [Message]
  const reply: Message = {
    ...person, id: '3', role: 'assistant', body: '', state: null, replyTo: '2', deliveredAt: null,
    question: { question: 'Which database?', choices: ['Postgres', 'MySQL'], fields: [] },
  }
  const asked: Conversation = { ...conversation, messages: [{ ...person, state: 'replied' }, reply] }
  const fields: Field[] = [
    { field: 'text', name: 'title', label: 'Title', helpText: '', isRequired: false, value: '', placeholder: '' },
    { field: 'number', name: 'budget', label: 'Budget', helpText: '', isRequired: false, value: null, minimum: null, maximum: null, step: null },
    { field: 'select', name: 'where', label: 'Where', helpText: '', isRequired: false, options: [{ value: 'staging', label: 'Staging' }, { value: 'production', label: 'Production' }], value: '' },
    { field: 'multi_select', name: 'tags', label: 'Tags', helpText: '', isRequired: false, options: [{ value: 'rack', label: 'Rack' }, { value: 'shelf', label: 'Shelf' }, { value: 'bin', label: 'Bin' }], value: [] },
    { field: 'radio', name: 'size', label: 'Size', helpText: '', isRequired: false, options: [{ value: 's', label: 'Small' }, { value: 'l', label: 'Large' }], value: '' },
    { field: 'checkbox', name: 'notify', label: 'Notify the owner', helpText: '', isRequired: false, value: false },
  ]
  const emptyValues = { title: ' ', budget: null, where: '', tags: [], size: '', notify: false }

  it('takes the first saved message of the person after the reply as the answer, after a save and in a new snapshot', () => {
    let state = conversationReducer(initialConversation, { type: 'snapshot', ...asked })
    expect(questionAnswer(state.conversation!.messages, reply)).toBeUndefined()
    const note: Message = { ...person, id: '4', body: '[Internal: Run 7 ended.]', isInternal: true, createdAt: '2026-09-18T10:01:00Z' }
    state = conversationReducer(state, { type: 'saved', message: note })
    expect(questionAnswer(state.conversation!.messages, reply)).toBeUndefined()
    const answer: Message = { ...person, id: '5', body: 'Neither, use SQLite.', state: 'pending', createdAt: '2026-09-18T10:02:00Z' }
    state = conversationReducer(state, { type: 'saved', message: answer })
    expect(questionAnswer(state.conversation!.messages, reply)).toEqual(answer)
    const later: Message = { ...answer, id: '6', body: 'Postgres', createdAt: '2026-09-18T10:03:00Z' }
    state = conversationReducer(state, { type: 'snapshot', ...asked, messages: [...asked.messages, note, answer, later] })
    expect(questionAnswer(state.conversation!.messages, reply)?.id).toBe(answer.id)
  })

  it('takes a message that the person wrote while the agent replied as the answer, and the next question takes the next message', () => {
    const queued: Message = { ...person, id: '4', body: 'Postgres, I think.', state: 'pending', createdAt: '2026-09-18T10:00:30Z' }
    const late: Message = { ...reply, createdAt: '2026-09-18T10:01:00Z' }
    const next: Message = { ...reply, id: '5', replyTo: queued.id, createdAt: '2026-09-18T10:02:00Z' }
    const click: Message = { ...person, id: '6', body: 'MySQL', state: 'pending', createdAt: '2026-09-18T10:03:00Z' }
    const messages = [person, queued, late, next, click]
    expect(questionAnswer(messages, late)).toEqual(queued)
    expect(questionAnswer(messages, next)).toEqual(click)
  })

  it('writes a form answer as the lines of a Slack Submit, with 0, option labels, Yes, No, and (no answer)', () => {
    expect(formAnswer(fields, { title: '  Fix the gate ', budget: 0, where: 'production', tags: ['shelf', 'rack'], size: 'l', notify: true })).toBe(
      'Title: Fix the gate\nBudget: 0\nWhere: Production\nTags: Rack, Shelf\nSize: Large\nNotify the owner: Yes',
    )
    expect(formAnswer(fields, emptyValues)).toBe(
      'Title: (no answer)\nBudget: (no answer)\nWhere: (no answer)\nTags: (no answer)\nSize: (no answer)\nNotify the owner: No',
    )
  })

  it('needs an answer in each required field and a check in each required checkbox, and takes 0', () => {
    const requiredFields = fields.map((field) => ({ ...field, isRequired: true }))
    expect(formErrors(requiredFields, emptyValues)).toEqual({
      title: 'Answer "Title".', budget: 'Answer "Budget".', where: 'Answer "Where".',
      tags: 'Answer "Tags".', size: 'Answer "Size".', notify: 'Check "Notify the owner".',
    })
    expect(formErrors(requiredFields, { title: 'Fix', budget: 0, where: 'staging', tags: ['bin'], size: 's', notify: true })).toEqual({})
    expect(formErrors(fields, emptyValues)).toEqual({})
  })
})
