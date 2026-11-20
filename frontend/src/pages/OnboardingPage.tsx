import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ArrowRight, Circle, CircleCheck, MessageSquare, ShieldCheck } from 'lucide-react'
import { Link, useLocation } from 'wouter'

import { chatApi } from '../chat/api'
import { AppGlyph } from '../components/AppGlyph'
import { Page } from '../components/Page'
import { PageHeading } from '../components/PageHeading'
import { useOnboardingProgress } from '../lib/useOnboardingProgress'
import brandLockupVertical from '../assets/brand-lockup-vertical.svg?raw'
import './onboarding-page.css'

const tasks = [
  {
    id: 'first-task',
    title: 'Start with one task',
    description: 'Choose one thing you want done. Druks will guide you through only the setup it needs.',
    icon: <MessageSquare aria-hidden="true" />,
  },
  {
    id: 'security-sweep',
    title: 'Set up a Security Sweep',
    description: 'Review security alerts and decide what Druks should do next.',
    icon: <ShieldCheck aria-hidden="true" />,
    request: 'Help me set up a Security Sweep.',
  },
  {
    id: 'software-factory',
    title: 'Set up Software Factory',
    description: 'Explore a guided workflow for taking a work item toward a pull request.',
    icon: <AppGlyph name="factory" size={24} />,
    request: 'Help me set up Software Factory.',
  },
]

export function OnboardingPage() {
  const [, navigate] = useLocation()
  const [selectedTask, setSelectedTask] = useState(tasks[0]!)
  const { providerReady } = useOnboardingProgress()
  const start = useMutation({
    mutationFn: (request: string) => chatApi.create(request),
    onSuccess: (conversation) => navigate(`/chat/${conversation.id}`),
  })

  return (
    <Page className="page-onboarding" scroll="page">
      <div className="onboarding-page-topbar">
        <Link href="/chat/new" className="druks-button">
          Skip tutorial
        </Link>
      </div>

      <section className="onboarding-page-content" aria-label="Your Druks onboarding">
        <section className="onboarding-welcome" aria-label="Welcome to Druks">
          <article className="druks-chat-message">
            <div className="druks-chat-message__meta">
              <span className="brand-lockup onboarding-welcome-logo" role="img" aria-label="Druks" dangerouslySetInnerHTML={{ __html: brandLockupVertical }} />
            </div>
            <div className="druks-chat-message__content druks-prose">
              <p>What would you like to get done? Choose a starting point below. Druks will guide you through the setup it needs and bring decisions back to you.</p>
            </div>
          </article>
        </section>

        <section className="onboarding-first-step" aria-labelledby="onboarding-step-title">
          <header className="onboarding-step-heading">
            <p className="onboarding-step-kicker"><span className="druks-badge druks-badge--featured">Step 1</span></p>
            <PageHeading
              titleId="onboarding-step-title"
              title="What should Druks help you do first?"
              description="Start with one task, check security alerts, or work toward a pull request."
              className="onboarding-page-heading"
            />
          </header>

          <div className="onboarding-task-list" role="group" aria-label="Choose a starting task">
            {tasks.map((task) => (
              <button
                type="button"
                key={task.id}
                className="onboarding-task druks-panel"
                aria-pressed={selectedTask === task}
                onClick={() => setSelectedTask(task)}
              >
                <span className="onboarding-task-icon">{task.icon}</span>
                <span className="onboarding-task-copy">
                  <span className="onboarding-task-title">{task.title}</span>
                  <span className="onboarding-task-description">{task.description}</span>
                </span>
                <span className="onboarding-task-indicator" aria-hidden="true">
                  {selectedTask === task
                    ? <>Selected <CircleCheck aria-hidden="true" /></>
                    : <>Choose <Circle aria-hidden="true" /></>}
                </span>
              </button>
            ))}
          </div>

          <div className="onboarding-step-footer">
            {providerReady === false ? <>
              <p>Druks needs an AI provider before it can reply.</p>
              <Link href="/settings/providers" className="druks-button druks-button--primary">
                Connect an AI provider <ArrowRight size={16} aria-hidden="true" />
              </Link>
            </> : <>
              <p>We’ll help you connect only what this task needs.</p>
              <button
                type="button"
                className="druks-button druks-button--primary"
                disabled={start.isPending}
                onClick={() => selectedTask.request ? start.mutate(selectedTask.request) : navigate('/chat/new')}
              >
                Continue with this task <ArrowRight size={16} aria-hidden="true" />
              </button>
            </>}
          </div>
          {start.isError && <p className="onboarding-start-error" role="alert">Could not start the conversation. {start.error.message}</p>}
        </section>
      </section>
    </Page>
  )
}
