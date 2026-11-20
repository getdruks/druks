import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'

export function useOnboardingProgress() {
  const subscriptions = useQuery({
    queryKey: ['providerSubscriptions'],
    queryFn: api.providerSubscriptions,
    staleTime: 60_000,
  })
  const keys = useQuery({
    queryKey: ['providerKeys'],
    queryFn: api.providerKeys,
    staleTime: 60_000,
  })
  const overview = useQuery({
    queryKey: ['dashboard', 'overview', ''],
    queryFn: () => api.dashboardOverview(),
    staleTime: 30_000,
  })

  const providerConnected = Boolean(
    subscriptions.data?.some((subscription) => subscription.connected && !subscription.revokedAt)
      || keys.data?.length,
  )
  const providerReady = providerConnected
    ? true
    : subscriptions.isPending || keys.isPending || subscriptions.isError || keys.isError
      ? null
      : false
  const overviewData = overview.data
  const hasFirstTask = Boolean(overviewData && (
    overviewData.needsYou.total || overviewData.running.total || overviewData.failed.total
      || overviewData.lastFinishedAt || overviewData.lastFailedAt
  ))
  const firstTaskStarted = hasFirstTask
    ? true
    : overview.isPending || overview.isError
      ? null
      : false

  return {
    checking: subscriptions.isPending || keys.isPending || overview.isPending,
    unavailable: providerReady === null || firstTaskStarted === null,
    providerReady,
    steps: [
      { label: 'Connect an AI provider', complete: providerReady, href: '/settings/providers' },
      { label: 'Send your first task', complete: firstTaskStarted, href: '/onboarding' },
    ],
  }
}
