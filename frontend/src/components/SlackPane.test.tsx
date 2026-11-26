import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { SlackPane } from './SlackPane'

const slack = { slug: 'slack', title: 'Slack', connected: true, facts: { team: 'Acme', bot_name: 'druks' } }
const ana = { id: 'grant-1', provider: 'slack', identity: { subject: 'U100', name: 'ana' }, revokedAt: null }

function renderPane(
  connections: object[],
  automations: { id: string; name: string; isAccepted: boolean }[] = [],
) {
  const fetchMock = vi.fn(async (url: string, request?: RequestInit) => {
    if (url === '/api/services') return new Response(JSON.stringify([slack]))
    if (url === '/api/oauth/connections') return new Response(JSON.stringify(connections))
    if (url === '/api/oauth/connections/grant-1' && request?.method === 'DELETE') {
      connections.length = 0
      return new Response(null, { status: 204 })
    }
    if (url === '/api/chat/services/slack/automations') {
      return new Response(JSON.stringify(automations))
    }
    if (url === '/api/chat/services/slack/automations/a1/accept' && request?.method === 'POST') {
      automations[0]!.isAccepted = true
      return new Response(JSON.stringify(automations[0]))
    }
    if (url === '/api/chat/services/slack/automations/a1' && request?.method === 'DELETE') {
      automations.length = 0
      return new Response(null, { status: 204 })
    }
    return new Response('{}', { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SlackPane />
    </QueryClientProvider>,
  )
  return fetchMock
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('shows the workspace, the bot, and a connect link that comes back to this page', async () => {
  window.history.replaceState(null, '', '/apps/chat/settings/channels')
  renderPane([])

  expect(await screen.findByText('Acme')).toBeTruthy()
  expect(screen.getByText('druks')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Connect Slack' }).getAttribute('href')).toBe(
    '/api/oauth/slack/connect?next=%2Fapps%2Fchat%2Fsettings%2Fchannels',
  )
})

it('shows the connected Slack account and disconnects it', async () => {
  const fetchMock = renderPane([ana])

  expect(await screen.findByText('ana')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))

  expect(await screen.findByRole('link', { name: 'Connect Slack' })).toBeTruthy()
  expect(fetchMock.mock.calls.some(([url, request]) =>
    url === '/api/oauth/connections/grant-1' && request?.method === 'DELETE',
  )).toBe(true)
})

it('accepts an automation that waits for an admin, then removes it', async () => {
  renderPane([ana], [{ id: 'a1', name: 'Support form', isAccepted: false }])

  expect(await screen.findByText('Support form')).toBeTruthy()
  expect(screen.getByText('Waits for an admin')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Accept' }))

  expect(await screen.findByText('Accepted')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }))

  await waitFor(() => expect(screen.queryByText('Support form')).toBeNull())
})
