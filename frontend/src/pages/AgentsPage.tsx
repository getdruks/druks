import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation } from 'wouter'

import { api } from '../api/client'
import type { Account, Agent } from '../api/types'
import { Button, Field, Select, Textarea, TextInput } from '../components/Control'
import { Page } from '../components/Page'
import { buildCatalog, knownProviders } from '../components/settings'
import { ModelChooser, Switch } from '../components/SettingsPanes'
import '../agents.css'

// Slack shows only these types as a reply's icon.
const PICTURE_TYPES = ['image/png', 'image/jpeg', 'image/gif']

export function AgentsPage({ account }: { account: Account }) {
  const [, navigate] = useLocation()
  const queryClient = useQueryClient()
  const agentsQuery = useQuery({ queryKey: ['agents'], queryFn: api.agents })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const skillsQuery = useQuery({ queryKey: ['skills'], queryFn: api.skillCollections })
  const harnessesQuery = useQuery({ queryKey: ['harnesses'], queryFn: api.harnesses })
  const providersQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })
  const subscriptionsQuery = useQuery({
    queryKey: ['providerSubscriptions'],
    queryFn: api.providerSubscriptions,
  })
  const keysQuery = useQuery({ queryKey: ['providerKeys'], queryFn: api.providerKeys })
  const catalogsQuery = useQuery({ queryKey: ['providerCatalogs'], queryFn: api.providerCatalogs })
  const isAdmin = account.role === 'admin'
  // Without a Slack connection the list fails, and the choice offers only no channel.
  const channelsQuery = useQuery({
    queryKey: ['notificationChannels'],
    queryFn: api.listNotificationChannels,
    enabled: isAdmin,
    retry: false,
  })
  const [draft, setDraft] = useState<Omit<Agent, 'id'> | null>(null)
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const agents = agentsQuery.data ?? []
  const accounts = accountsQuery.data ?? []
  const usernames = new Map(accounts.map((entry) => [entry.id, entry.username]))
  const channels = channelsQuery.data ?? []
  const skills = (skillsQuery.data ?? [])
    .flatMap((collection) => collection.skills)
    .filter((skill) => skill.enabled)
  const harnesses = harnessesQuery.data ?? []
  // An agent answers in Chat. The form offers only the harnesses that Chat runs on.
  const chatHarnesses = harnesses.filter((harness) => harness.hasChat)
  const catalogs = catalogsQuery.data ?? []
  const catalog = buildCatalog(
    harnesses,
    knownProviders(providersQuery.data ?? [], catalogs),
    catalogs,
    subscriptionsQuery.data ?? [],
    keysQuery.data ?? [],
  )
  const formQueries = [
    accountsQuery,
    skillsQuery,
    harnessesQuery,
    providersQuery,
    subscriptionsQuery,
    keysQuery,
    catalogsQuery,
  ]
  const formReady = formQueries.every((query) => query.isSuccess)

  function addAgent() {
    setDraft({
      name: '',
      description: '',
      instructions: '',
      skills: [],
      harness: chatHarnesses.find((harness) => harness.billingOptions.includes('subscription'))?.name ?? '',
      model: '',
      isPrivate: true,
      ownerId: account.id,
      channelId: null,
      picture: null,
    })
    setEditingAgentId(null)
    setError(null)
  }

  function editAgent(agent: Agent) {
    const { id, ...input } = agent
    setDraft(input)
    setEditingAgentId(id)
    setError(null)
  }

  function chooseSkill(name: string, on: boolean) {
    setDraft((current) =>
      current
        ? {
            ...current,
            skills: on ? [...current.skills, name] : current.skills.filter((skill) => skill !== name),
          }
        : current,
    )
  }

  async function uploadPicture(file: File) {
    if (!PICTURE_TYPES.includes(file.type)) {
      setError(`${file.name} is not a png, jpg or gif image. Choose one of these types.`)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const picture = await api.upload('core', file)
      setDraft((current) => (current ? { ...current, picture } : current))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function saveAgent(agent: Omit<Agent, 'id'>) {
    setBusy(true)
    setError(null)
    try {
      const { ownerId, channelId, picture, ...input } = agent
      if (editingAgentId)
        await api.updateAgent(editingAgentId, {
          ...input,
          ownerId,
          channelId,
          picture: picture?.id ?? null,
        })
      else await api.createAgent(input)
      await queryClient.invalidateQueries({ queryKey: ['agents'] })
      setDraft(null)
      setEditingAgentId(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function deleteAgent(agent: Agent) {
    if (window.confirm(`Delete agent ${agent.name}? This also deletes every conversation with it and stops their schedules.`)) {
      setBusy(true)
      setError(null)
      try {
        await api.deleteAgent(agent.id)
        await queryClient.invalidateQueries({ queryKey: ['agents'] })
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setBusy(false)
      }
    }
  }

  // A private agent runs on its owner's subscription. A public agent can run on the install's API key.
  const billing = draft?.isPrivate ? 'subscription' : 'api_key'
  const harness = harnesses.find((entry) => entry.name === draft?.harness)
  // The form sees only the viewer's subscriptions and the installation's keys. A public agent without a key runs on the unattended account's subscription.
  const canCheckCredential = draft?.isPrivate ? draft.ownerId === account.id : !harness?.billingOptions.includes('subscription')
  // A saved skill that Settings no longer enables stays listed, so the person can turn it off and save.
  const skillChoices = [
    ...skills,
    ...(draft?.skills ?? [])
      .filter((name) => !skills.some((skill) => skill.name === name))
      .map((name) => ({ name, description: 'Not enabled in Settings → Skills. Turn it off to save.' })),
  ]

  const errorAlert = error && (
    <div className="mcp-error" role="alert">
      {error}
    </div>
  )

  return (
    <Page inset className="agents-page">
      <header className="agents-head">
        <div>
          <h1>Agents</h1>
          <p>
            An agent has its own instructions, skills and model. A private agent shows only to its
            owner and the admins.
          </p>
        </div>
        <div className="mcp-actions">
          <Button variant="primary" onClick={addAgent} disabled={busy || Boolean(draft) || !formReady}>
            Add agent
          </Button>
        </div>
      </header>

      {agentsQuery.isPending && <p role="status">Loading agents…</p>}
      {agentsQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load agents.{' '}
          <Button onClick={() => void agentsQuery.refetch()}>Try again</Button>
        </p>
      )}
      {formQueries.some((query) => query.isError) && (
        <p className="mcp-error" role="alert">
          Could not load the people, skills and models.{' '}
          <Button onClick={() => formQueries.forEach((query) => void query.refetch())}>
            Try again
          </Button>
        </p>
      )}
      {!draft && errorAlert}

      {draft && (
        <section
          className="druks-panel mcp-custom-body"
          aria-label={editingAgentId ? 'Edit agent' : 'Add agent'}
        >
          <Field label="Name">
            <TextInput
              aria-label="Agent name"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              disabled={busy}
            />
          </Field>
          <Field label="Description">
            <TextInput
              aria-label="Description"
              value={draft.description}
              onChange={(event) => setDraft({ ...draft, description: event.target.value })}
              disabled={busy}
            />
          </Field>
          <Field label="Instructions">
            <Textarea
              aria-label="Instructions"
              rows={8}
              value={draft.instructions}
              onChange={(event) => setDraft({ ...draft, instructions: event.target.value })}
              disabled={busy}
            />
          </Field>
          <Field
            label="Skills"
            help={isAdmin ? 'Enable more skills in Settings → Skills.' : undefined}
          >
            <div className="set-app-toggles">
              {skillChoices.map((skill) => {
                const on = draft.skills.includes(skill.name)
                return (
                  <div key={skill.name} className="set-app-toggle">
                    <div className="mt-text">
                      <span className="mt-name">{skill.name}</span>
                      <span className="mt-desc">{skill.description}</span>
                    </div>
                    <Switch
                      on={on}
                      onClick={() => chooseSkill(skill.name, !on)}
                      disabled={busy}
                      label={skill.name}
                    />
                  </div>
                )
              })}
              {skillChoices.length === 0 && <p>No skills are enabled.</p>}
            </div>
          </Field>
          <Field label="Private">
            <Switch
              on={draft.isPrivate}
              onClick={() => setDraft({ ...draft, isPrivate: !draft.isPrivate })}
              disabled={busy}
              label="Private"
            />
          </Field>
          <Field label="Harness">
            <Select
              aria-label="Harness"
              value={draft.harness}
              onChange={(event) => setDraft({ ...draft, harness: event.target.value, model: '' })}
              disabled={busy}
            >
              {chatHarnesses.map((harness) => (
                <option
                  key={harness.name}
                  value={harness.name}
                  disabled={!harness.billingOptions.includes(billing)}
                >
                  {harness.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Model"
            help="A private agent runs on its owner's subscription. A public agent runs on the installation's API key or, without one, on the unattended account's subscription. Connect them in Settings → Providers."
          >
            <ModelChooser
              value={draft.model || null}
              displayValue={draft.model || 'Choose a model'}
              choices={catalog
                .modelsOf(draft.harness, billing)
                .map((choice) => (canCheckCredential ? choice : { ...choice, enabled: true, unavailableReason: undefined }))}
              onPick={(model) => model && setDraft({ ...draft, model })}
              disabled={busy}
              label="Model"
              onAddProvider={() => navigate('/settings/providers')}
            />
          </Field>
          <Field label="Owner">
            {isAdmin && editingAgentId ? (
              <Select
                aria-label="Owner"
                value={draft.ownerId}
                onChange={(event) => setDraft({ ...draft, ownerId: event.target.value })}
                disabled={busy}
              >
                {accounts.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.username}
                  </option>
                ))}
              </Select>
            ) : (
              <span>{usernames.get(draft.ownerId)}</span>
            )}
          </Field>
          {editingAgentId && (
            <Field label="Picture" help="A png, jpg or gif image. Slack shows it on the agent's replies.">
              {draft.picture && (
                <div className="agent-picture">
                  <img src={draft.picture.url} alt={`Picture of ${draft.name}`} />
                  <Button onClick={() => setDraft({ ...draft, picture: null })} disabled={busy}>
                    Remove picture
                  </Button>
                </div>
              )}
              <TextInput
                // A new identity drops the file the browser still holds, so the same file
                // can be picked again after Remove picture.
                key={draft.picture?.id ?? 'none'}
                aria-label="Picture"
                type="file"
                accept={PICTURE_TYPES.join(',')}
                onChange={(event) => event.target.files?.[0] && uploadPicture(event.target.files[0])}
                disabled={busy}
              />
            </Field>
          )}
          {isAdmin && editingAgentId && (
            <Field
              label="Slack channel"
              help="A tag that names no agent in this channel reaches this agent. Only a public agent can have one. Invite the bot to a channel to see it here."
            >
              <Select
                aria-label="Slack channel"
                value={draft.channelId ?? ''}
                onChange={(event) => setDraft({ ...draft, channelId: event.target.value || null })}
                disabled={busy}
              >
                <option value="">No channel</option>
                {draft.channelId &&
                  !channels.some((channel) => channel.id === draft.channelId) && (
                    <option value={draft.channelId}>{draft.channelId}</option>
                  )}
                {channels.map((channel) => (
                  <option key={channel.id} value={channel.id}>
                    #{channel.name}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          {errorAlert}
          <div className="mcp-actions">
            <Button
              onClick={() => {
                setDraft(null)
                setEditingAgentId(null)
                setError(null)
              }}
              disabled={busy}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void saveAgent(draft)}
              disabled={busy || !draft.name.trim() || !draft.model}
            >
              {editingAgentId ? 'Save agent' : 'Create agent'}
            </Button>
          </div>
        </section>
      )}

      {agentsQuery.isSuccess && agents.length === 0 && <p>There are no agents yet.</p>}
      {agents.length > 0 && (
        <table className="connections-table agents-table" aria-label="Agents">
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">Description</th>
              <th scope="col">Owner</th>
              <th scope="col">Visibility</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id}>
                <th scope="row">
                  <Link className="connection-name" href={`/agents/${agent.id}`}>
                    {agent.name}
                  </Link>
                </th>
                <td data-label="Description">{agent.description}</td>
                <td data-label="Owner">{usernames.get(agent.ownerId)}</td>
                <td data-label="Visibility">{agent.isPrivate ? 'Private' : 'Public'}</td>
                <td className="connection-actions">
                  {(isAdmin || agent.ownerId === account.id) && (
                    <div className="mcp-actions">
                      <Button
                        onClick={() => editAgent(agent)}
                        disabled={busy || Boolean(draft) || !formReady}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="danger"
                        onClick={() => void deleteAgent(agent)}
                        disabled={busy || Boolean(draft)}
                      >
                        Delete
                      </Button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  )
}
