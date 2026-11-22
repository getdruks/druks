import { ArrowRight, Book, Circle, CircleCheck } from 'lucide-react'
import { Link } from 'wouter'

import { useOnboardingProgress } from '../lib/useOnboardingProgress'
import './onboarding-progress.css'

export function OnboardingNavigationItem() {
  const { checking, unavailable, steps } = useOnboardingProgress()
  const completed = steps.filter((step) => step.complete === true).length

  return (
    <Link href="/onboarding" className="sidebar-link">
      <Book size={17} aria-hidden="true" />
      <span>Onboarding</span>
      <span className="onboarding-navigation-count">{checking ? '…' : unavailable ? '—' : `${completed} of ${steps.length}`}</span>
    </Link>
  )
}

export function CurrentMilestoneCard() {
  const { checking, unavailable, steps } = useOnboardingProgress()
  const completed = steps.filter((step) => step.complete === true).length
  const allComplete = !unavailable && completed === steps.length

  return (
    <article className="connection-card onboarding-milestone-card druks-panel">
      <div className="onboarding-milestone-content">
        <div className="onboarding-milestone-copy">
          <div className="onboarding-milestone-title-row">
            <h2>{allComplete ? 'You’re ready to explore' : 'Get Druks ready'}</h2>
            <p className="onboarding-progress" aria-live="polite">
              {checking ? 'Checking…' : unavailable ? 'Status unavailable' : `${completed} of ${steps.length}`}
            </p>
          </div>
          <p>{allComplete ? 'Your first task is complete.' : 'Connect a provider, then give Druks a first task.'}</p>
        </div>
        <ul className="onboarding-checklist">
          {steps.map((step) => (
            <li key={step.label}>
              {step.complete === true
                ? <CircleCheck className="onboarding-checklist-icon is-complete" aria-hidden="true" />
                : <Circle className="onboarding-checklist-icon" aria-hidden="true" />}
              <span>{step.label}</span>
              {step.complete === null && !checking && <span className="onboarding-step-status">Couldn’t check</span>}
              {step.complete === false && (
                <Link
                  className="druks-button druks-button--primary druks-button--compact druks-button--icon-only"
                  href={step.href}
                  aria-label={`Go to ${step.label}`}
                >
                  <ArrowRight aria-hidden="true" />
                </Link>
              )}
            </li>
          ))}
        </ul>
      </div>
    </article>
  )
}
