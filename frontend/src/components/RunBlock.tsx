import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { api, subjectApi } from '../api/client'
import type { RunSummary } from '../api/types'
import { AppPagesProvider } from '../druksui/AppPage'
import { GateControls } from '../druksui/GateControls'
import { absTime, httpUrl, relTimeFromIso } from '../lib/format'
import { isActiveRun } from '../lib/states'
import { CancelRun, RetryRun } from './RunControls'
import { RunTranscript } from './RunTranscript'
import { StatusGlyph } from './StatusGlyph'

export function RunBlock({
  run,
  newest,
  selected,
  expected,
  onRetried,
}: {
  run: RunSummary
  newest: boolean
  selected: boolean
  expected?: string
  onRetried?: (runId: string) => void
}) {
  const ask = run.state === 'parked' ? run.inputRequest : null
  const call = run.agentCalls.at(-1)
  const [open, setOpen] = useState(false)
  const sandboxes = useQuery({
    // The state is in the key, so that a run that ends asks once more. Its last box
    // can be newer than the last answer.
    queryKey: ['runSandboxes', run.id, run.state],
    queryFn: () => api.listRunSandboxes(run.id),
    placeholderData: keepPreviousData,
    // A run gets its boxes while it runs, and no stream carries them.
    refetchInterval: run.state === 'running' ? 5_000 : false,
  })
  const block = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (selected) {
      block.current?.focus({ preventScroll: true })
      block.current?.scrollIntoView({ block: 'start' })
    }
  }, [selected])
  return (
    <div
      className="subject-run mono"
      ref={block}
      tabIndex={-1}
      aria-label={`${run.label} run ${run.id}`}
    >
      <div className="subject-run-head">
        <StatusGlyph state={run.state} />
        <span>{run.label}</span>
        <span className="dim">{run.state}</span>
        <span className="dim">{relTimeFromIso(run.updatedAt)}</span>
        {isActiveRun(run) && <CancelRun runId={run.id} />}
        {run.state === 'failed' && newest && <RetryRun runId={run.id} onRetried={onRetried} />}
      </div>
      {ask?.presentation !== 'external' && (expected || ask?.presentation === 'in_app') && (
        <AppPagesProvider app={run.app}>
          <GateControls run={run.id} expected={expected} />
        </AppPagesProvider>
      )}
      {ask?.presentation === 'external' && (
        <div className="ins-needs">
          <div className="ins-needs-k">
            <span>◆</span> needs you
          </div>
          <div className="ins-needs-body">
            {ask.label ?? 'This run needs your input.'}
            {httpUrl(ask.url) && (
              <>
                {' '}
                <a className="ins-link" href={httpUrl(ask.url)} target="_blank" rel="noreferrer">
                  Open request ↗
                </a>
              </>
            )}
          </div>
        </div>
      )}
      {run.state === 'failed' && run.failure && (
        <div className="ins-fail">
          <div className="ins-fail-k">
            <span>✕</span> failed
          </div>
          <div className="ins-fail-body">{run.failure}</div>
        </div>
      )}
      {sandboxes.data?.map((sandbox, index) => (
        <div className="subject-run-secrets" key={index}>
          <div className="dim">
            secrets fetched from Druks ·{' '}
            {sandbox.scopedTo === 'workflow' ? 'whole run' : sandbox.scopedTo} ·{' '}
            {sandbox.isLive ? 'sandbox live' : 'no live sandbox'}
          </div>
          {sandbox.secrets.map((secret) => (
            <div key={secret.name}>
              {secret.name}{' '}
              <span className="dim">
                {secret.kind} {secret.secretId}
              </span>{' '}
              {secret.resources.join(', ')}
              {secret.issues.map((issue) => (
                <div className="subject-run-issue dim" key={issue.issuedAt}>
                  {absTime(issue.issuedAt)} UTC ·{' '}
                  {issue.error
                    ? `no token: ${issue.error}`
                    : [
                        issue.fingerprint ? `token ${issue.fingerprint}` : 'value with no expiry',
                        issue.expiresAt && `expires ${absTime(issue.expiresAt)}`,
                        issue.mintedAt && `minted ${absTime(issue.mintedAt)}`,
                        issue.audience,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
      {call && (
        <button
          type="button"
          className="ins-run-link subject-run-more druks-button druks-button--quiet druks-button--compact"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {open ? '▾' : '▸'} transcript
        </button>
      )}
      {open && call && (
        <RunTranscript
          basePath={subjectApi.transcriptBase(run.app, call.id)}
          isLive={call.status === 'running'}
        />
      )}
      {run.state === 'failed' && !call && !run.failure && (
        <div className="ins-fail-body dim">failed before any agent step</div>
      )}
    </div>
  )
}
