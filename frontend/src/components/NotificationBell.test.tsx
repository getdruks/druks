import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { NotificationResponse } from '../api/types'
import { NotificationBell } from './NotificationBell'

vi.mock('../api/client', () => ({
  api: { listNotifications: vi.fn(), markNotificationsRead: vi.fn(), getPersonalSettings: vi.fn(), listConnections: vi.fn() },
}))

const disconnect: NotificationResponse = {
  id: 'n1',
  body: 'Your Anthropic subscription is disconnected.',
  deepLink: 'https://druks.example/settings/providers',
  readAt: null,
  createdAt: '2026-11-17T09:00:00Z',
}

function renderBell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <NotificationBell />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('NotificationBell', () => {
  it('counts the unread notifications and marks them read when opened', async () => {
    vi.mocked(api.listNotifications)
      .mockResolvedValueOnce([disconnect])
      .mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    vi.mocked(api.markNotificationsRead).mockResolvedValue()
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications, 1 unread' }))

    await waitFor(() => expect(api.markNotificationsRead).toHaveBeenCalledOnce())
    expect(await screen.findByRole('button', { name: 'Notifications' })).toBeTruthy()
  })

  it('links a notification to what it is about', async () => {
    vi.mocked(api.listNotifications).mockResolvedValue([{ ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const link = await screen.findByRole('link', { name: disconnect.body })
    expect(link.getAttribute('href')).toBe(disconnect.deepLink)
    expect(api.markNotificationsRead).not.toHaveBeenCalled()
  })

  it('shows an approval by its label beside the notices that it keeps whole', async () => {
    const approval: NotificationResponse = {
      id: 'n2',
      body: '# Review: Plan\n\nThe plan changes the schema.\n\nWhich database?\n- Postgres (recommended)\n\nShip it.',
      deepLink: 'https://druks.example/runs/run-1',
      readAt: '2026-11-17T09:05:00Z',
      createdAt: '2026-11-17T09:00:00Z',
    }
    vi.mocked(api.listNotifications).mockResolvedValue([approval, { ...disconnect, readAt: '2026-11-17T09:05:00Z' }])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const rows = await screen.findAllByRole('link')
    expect(rows.map((row) => [row.textContent, row.getAttribute('href')])).toEqual([
      ['Review: Plan', approval.deepLink],
      [disconnect.body, disconnect.deepLink],
    ])
    expect(screen.queryByText(/Which database/)).toBeNull()
  })

  it('names the app and what a message about a run is about before its label or its failure', async () => {
    const approval: NotificationResponse = {
      ...disconnect,
      id: 'n3',
      readAt: '2026-11-17T09:05:00Z',
      body: '### Security Sweep\n\nacme/api\n\n# Add a dependabot.yml?\n\nSkip, and later sweeps will not offer it again.',
    }
    const failure: NotificationResponse = {
      ...approval,
      id: 'n4',
      body: '### Inbox Manager\n\nJoin us for a live demo\n\nFailed: Nobody answered the question in time.',
    }
    const subjectless: NotificationResponse = {
      ...approval,
      id: 'n5',
      body: '### Usage\n\nFailed: The provider is down.',
    }
    vi.mocked(api.listNotifications).mockResolvedValue([approval, failure, subjectless])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const rows = await screen.findAllByRole('link')
    expect(rows.map((row) => row.textContent)).toEqual([
      'Security Sweep · acme/api · Add a dependabot.yml?',
      'Inbox Manager · Join us for a live demo · Failed: Nobody answered the question in time.',
      'Usage · Failed: The provider is down.',
    ])
  })

  it('says how to choose a route to a person with no channel and no Slack account', async () => {
    vi.mocked(api.listNotifications).mockResolvedValue([])
    vi.mocked(api.getPersonalSettings).mockResolvedValue({
      timezone: 'UTC',
      theme: 'system',
      notificationChannelId: null,
    })
    vi.mocked(api.listConnections).mockResolvedValue([])
    renderBell()

    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))

    const link = await screen.findByRole('link', { name: 'Choose where notifications go' })
    expect(link.getAttribute('href')).toBe('/settings/personal?field=notifications')
  })
})
