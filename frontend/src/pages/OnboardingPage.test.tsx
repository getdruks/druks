import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Router } from 'wouter'

import { api } from '../api/client'
import type { ProviderKey } from '../api/types'
import { chatApi } from '../chat/api'
import type { Conversation } from '../chat/state'
import { OnboardingPage } from './OnboardingPage'

vi.mock('../api/client', () => ({ api: { providerSubscriptions: vi.fn(), providerKeys: vi.fn(), dashboardOverview: vi.fn() } }))
vi.mock('../chat/api', () => ({ chatApi: { create: vi.fn() } }))

function mount(keys: ProviderKey[]) {
  window.history.replaceState(null, '', '/onboarding')
  vi.mocked(api.providerSubscriptions).mockResolvedValue([])
  vi.mocked(api.providerKeys).mockResolvedValue(keys)
  vi.mocked(api.dashboardOverview).mockReturnValue(new Promise(() => {}))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Router><OnboardingPage /></Router></QueryClientProvider>)
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Onboarding page', () => {
  it('starts a Chat conversation that asks for the chosen task', async () => {
    vi.mocked(chatApi.create).mockResolvedValue({ id: 'c1' } as Conversation)
    mount([{ keyTail: '1234' } as ProviderKey])
    fireEvent.click(screen.getByRole('button', { name: /Set up a Security Sweep/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Continue with this task/ }))

    await waitFor(() => expect(window.location.pathname).toBe('/chat/c1'))
    expect(chatApi.create).toHaveBeenCalledWith('Help me set up a Security Sweep.')
  })

  it('sends a person with no AI provider to Providers first', async () => {
    mount([])

    expect((await screen.findByRole('link', { name: /Connect an AI provider/ })).getAttribute('href')).toBe('/settings/providers')
    expect(screen.queryByRole('button', { name: /Continue with this task/ })).toBeNull()
  })
})
