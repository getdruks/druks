import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { api, ApiError } from '../api/client'
import type { RunDetail } from '../api/types'
import { registerAppUI } from '../apps/registry'
import { RunPage } from './RunPage'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { getRun: vi.fn(), retryRun: vi.fn(), listRunSandboxes: vi.fn() },
}))

registerAppUI({
  name: 'run_page_notes',
  routes: [],
  subjectPath: ({ id }, target) => `/run_page_notes/notes/${id}?run=${target?.run}`,
})

const getRun = vi.mocked(api.getRun)
const listRunSandboxes = vi.mocked(api.listRunSandboxes)
const failed: RunDetail = {
  subject: null,
  run: {
    id: 'run-1', app: 'run_page_notes', kind: 'run_page_notes.digest', label: 'Digest', retryFrom: null, state: 'failed',
    failure: 'the disk is full', gate: null, createdAt: '2026-11-17T10:00:00Z',
    updatedAt: '2026-11-17T10:01:00Z', accountUsername: 'op@example.com', agentCalls: [],
  },
}

function mount() {
  const location = memoryLocation({ path: '/runs/run-1', record: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Router hook={location.hook}><RunPage id="run-1" /></Router>
    </QueryClientProvider>,
  )
  return location
}

beforeEach(() => { listRunSandboxes.mockResolvedValue([]) })
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('opens a run about a work item on the work item page with the run selected', async () => {
  getRun.mockResolvedValue({ ...failed, subject: { type: 'note', id: '7' } })
  const location = mount()
  await waitFor(() => expect(location.history.at(-1)).toBe('/run_page_notes/notes/7?run=run-1'))
  expect(location.history).toHaveLength(1)
})

it('shows a run about no work item with why it failed', async () => {
  getRun.mockResolvedValue(failed)
  mount()
  expect(await screen.findByText('the disk is full')).toBeTruthy()
  expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
})

it('opens the new run after a retry', async () => {
  getRun.mockResolvedValue(failed)
  vi.mocked(api.retryRun).mockResolvedValue({ run: 'run-2' })
  const location = mount()
  fireEvent.click(await screen.findByRole('button', { name: /retry/i }))
  await waitFor(() => expect(location.history.at(-1)).toBe('/runs/run-2'))
})

it('says so when the run does not exist', async () => {
  getRun.mockRejectedValue(new ApiError('run not found', 404, null))
  mount()
  expect((await screen.findByRole('alert')).textContent).toContain('does not exist')
})

it('lists the secrets that the sandbox of the run fetched', async () => {
  getRun.mockResolvedValue(failed)
  listRunSandboxes.mockResolvedValue([
    {
      scopedTo: 'workflow',
      isLive: false,
      secrets: [{
        name: 'github',
        secretId: 'vault-1',
        kind: 'app_key',
        resources: ['getdruks/druks'],
        issues: [
          { issuedAt: '2026-11-27T10:00:05Z', fingerprint: '1a2b3c4d', expiresAt: '2026-11-27T11:00:00Z', audience: 'resource:github/installation/77/druks', mintedAt: '2026-11-27T10:00:04Z', error: '' },
          { issuedAt: '2026-11-27T10:55:00Z', fingerprint: '', expiresAt: null, audience: '', mintedAt: null, error: 'GitHub refused' },
        ],
      }],
    },
  ])
  mount()
  expect((await screen.findByText(/app_key/)).textContent).toBe('app_key vault-1')
  expect(screen.getByText(/token 1a2b3c4d/).textContent).toBe(
    '2026-11-27 10:00:05 UTC · token 1a2b3c4d · expires 2026-11-27 11:00:00 · minted 2026-11-27 10:00:04 · resource:github/installation/77/druks',
  )
  expect(screen.getByText(/no token/).textContent).toBe('2026-11-27 10:55:00 UTC · no token: GitHub refused')
  expect(screen.getByText(/secrets fetched from Druks/).textContent).toContain('whole run · no live sandbox')
})

it('shows no secrets for a run with no sandbox', async () => {
  getRun.mockResolvedValue(failed)
  mount()
  await screen.findByText('the disk is full')
  await waitFor(() => expect(listRunSandboxes).toHaveBeenCalledWith('run-1'))
  expect(screen.queryByText(/secrets fetched from Druks/)).toBeNull()
})
