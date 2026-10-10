import { useMemo, useState } from "preact/hooks"
import type { AgentsModel } from "../models/agents"
import { callTypeLabel, CALL_TYPES, MAX_RETENTION_DAYS, MAX_SILENCE_MS, MIN_SILENCE_MS, type AgentFormModel, type Choice, type FormValues, type Stage } from "../models/agentForm"
import { IconPicker } from "./IconPicker"
import { Link } from "./Link"
import { agentsPath, navigate } from "./route"
import { useModel } from "./useModel"

const STAGE_TITLES = { stt: "Speech to text", tts: "Voice" } as const

function stageTitle(stage: Stage, oneWay: boolean): string {
  return stage === "action" ? (oneWay ? "Webhook" : "Responder") : STAGE_TITLES[stage]
}

function number(value: string): number {
  return value.trim() === "" ? Number.NaN : Number(value)
}

function shown(value: number): string | number {
  return Number.isNaN(value) ? "" : value
}

type Update = (patch: Partial<FormValues>) => void

/** Base URL, model and API key of an own speech to text, chat or voice service. */
function OpenAiFields({ form, stage }: { form: AgentFormModel; stage: Stage }) {
  const s = useModel(form)
  const keys = {
    stt: ["sttBaseUrl", "sttModel", "sttApiKey"],
    action: ["chatBaseUrl", "chatModel", "chatApiKey"],
    tts: ["ttsBaseUrl", "ttsModel", "ttsApiKey"],
  } as const
  const [url, model, key] = keys[stage]
  const title = stageTitle(stage, false)
  return (
    <>
      <label class="field">
        <span>{title} base URL (https://…/v1)</span>
        <input type="url" name={`${stage}-base-url`} value={s[url]} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ [url]: e.currentTarget.value })} />
      </label>
      <label class="field">
        <span>{title} model</span>
        <input type="text" name={`${stage}-model`} value={s[model]} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ [model]: e.currentTarget.value })} />
      </label>
      <label class="field">
        <span>{title} API key (optional)</span>
        <input type="password" name={`${stage}-api-key`} value={s[key]} autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ [key]: e.currentTarget.value })} />
      </label>
    </>
  )
}

function WebhookFields({ form }: { form: AgentFormModel }) {
  const s = useModel(form)
  return (
    <>
      <label class="field">
        <span>Webhook address (https://…)</span>
        <input type="url" name="webhook-url" value={s.webhookUrl} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ webhookUrl: e.currentTarget.value })} />
      </label>
      <label class="field">
        <span>Header name (optional)</span>
        <input type="text" name="webhook-header-name" value={s.webhookHeaderName} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ webhookHeaderName: e.currentTarget.value })} />
      </label>
      <label class="field">
        <span>Header value</span>
        <input type="password" name="webhook-header-value" value={s.webhookHeaderValue} autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ webhookHeaderValue: e.currentTarget.value })} />
      </label>
    </>
  )
}

function StageSection({ form, stage }: { form: AgentFormModel; stage: Stage }) {
  const s = useModel(form)
  const oneWay = form.isOneWay
  const choice = s[stage]
  return (
    <section class="stage">
      <label class="field">
        <span>{stageTitle(stage, oneWay)}</span>
        <select name={stage} value={choice} onChange={(e) => form.update({ [stage]: e.currentTarget.value as Choice })}>
          {form.choices(stage).map((option) => (
            <option key={option} value={option}>
              {form.label(option)}
            </option>
          ))}
        </select>
      </label>
      {choice === "custom" ? stage === "action" && oneWay ? <WebhookFields form={form} /> : <OpenAiFields form={form} stage={stage} /> : null}
    </section>
  )
}

function Advanced({ form }: { form: AgentFormModel }) {
  const s = useModel(form)
  const update: Update = (patch) => form.update(patch)
  const kind = s.retention === "default" ? "default" : s.retention === "forever" ? "forever" : "days"
  return (
    <details class="advanced">
      <summary>Advanced</summary>
      <label class="field">
        <span>Language</span>
        <input type="text" name="language" value={s.language} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => update({ language: e.currentTarget.value })} />
      </label>
      <label class="field">
        <span>Turn end</span>
        <select name="turn-end" value={s.turnEnd} onChange={(e) => update({ turnEnd: e.currentTarget.value })}>
          <option value="auto">Automatic</option>
          <option value="manual">Manual</option>
        </select>
      </label>
      <label class="field">
        <span>Silence before the turn ends (ms)</span>
        <input type="number" name="silence" inputmode="numeric" min={MIN_SILENCE_MS} max={MAX_SILENCE_MS} step={100} value={shown(s.silenceMs)} onInput={(e) => update({ silenceMs: number(e.currentTarget.value) })} />
      </label>
      <label class="field">
        <span>Keep history</span>
        <select
          name="retention"
          value={kind}
          onChange={(e) => {
            const value = e.currentTarget.value
            update({ retention: value === "days" ? 30 : value === "forever" ? "forever" : "default" })
          }}
        >
          <option value="default">Server default</option>
          <option value="days">For some days</option>
          <option value="forever">Forever</option>
        </select>
      </label>
      {typeof s.retention === "number" ? (
        <label class="field">
          <span>Days</span>
          <input type="number" name="retention-days" inputmode="numeric" min={1} max={MAX_RETENTION_DAYS} step={1} value={shown(s.retention as number)} onInput={(e) => update({ retention: number(e.currentTarget.value) })} />
        </label>
      ) : null}
      {!form.isOneWay ? (
        <>
          <label class="field">
            <span>Prompt</span>
            <textarea name="prompt" rows={5} value={s.systemPrompt} onInput={(e) => update({ systemPrompt: e.currentTarget.value })} />
          </label>
          <label class="field">
            <span>Failure message</span>
            <input type="text" name="fallback" value={s.fallbackMessage} placeholder="Sorry, I couldn't answer right now." onInput={(e) => update({ fallbackMessage: e.currentTarget.value })} />
          </label>
        </>
      ) : null}
    </details>
  )
}

function FormBody({ form, serverId }: { form: AgentFormModel; serverId: string }) {
  const s = useModel(form)
  const [error, setError] = useState<string | null>(null)
  const list = agentsPath(serverId)
  const message = form.validate()
  const oneWay = form.isOneWay
  const stages: Stage[] = oneWay ? ["stt", "action"] : ["stt", "action", "tts"]

  async function submit(): Promise<void> {
    if (s.saving) return
    setError(null)
    try {
      await form.save()
      navigate(list)
    } catch (e) {
      setError(e instanceof Error ? e.message : "The agent could not be saved.")
    }
  }

  return (
    <main class="screen">
      <p>
        <Link href={list}>Agents</Link>
      </p>
      <h1>{form.isEditing ? "Edit agent" : "New agent"}</h1>
      {/* noValidate: the model's own checks are the only ones (the browser's would refuse values the server accepts, such as a silence of 850 ms). */}
      <form
        class="fields"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <label class="field">
          <span>Name</span>
          <input type="text" name="name" value={s.displayName} enterkeyhint="next" onInput={(e) => form.update({ displayName: e.currentTarget.value })} />
        </label>
        {form.isEditing ? (
          <dl class="facts">
            <dt>Slug</dt>
            <dd>{s.slug}</dd>
          </dl>
        ) : (
          <label class="field">
            <span>Slug</span>
            <input type="text" name="slug" value={s.slug} autocapitalize="none" autocorrect="off" spellcheck={false} onInput={(e) => form.update({ slug: e.currentTarget.value })} />
          </label>
        )}

        <h2 class="section-title">Icon</h2>
        <IconPicker value={s.icon} onChange={(icon) => form.update({ icon })} />

        <label class="field">
          <span>Call type</span>
          <select name="call-type" value={s.callType} onChange={(e) => form.selectCallType(e.currentTarget.value)}>
            {CALL_TYPES.map((type) => (
              <option key={type} value={type}>
                {callTypeLabel(type)}
              </option>
            ))}
          </select>
        </label>
        <p class="footnote">{oneWay ? "The watch records you and sends the transcript to a webhook. There is no answer and no voice." : "You talk and the agent answers with a voice."}</p>

        {stages.map((stage) => (
          <StageSection key={stage} form={form} stage={stage} />
        ))}
        <Advanced form={form} />

        {message !== null ? (
          <p class="footnote" role="status">
            {message}
          </p>
        ) : null}
        {error !== null ? (
          <p class="problem" role="alert">
            {error}
          </p>
        ) : null}
        <div class="actions">
          <button class="button" type="submit" disabled={s.saving || message !== null || form.unchanged}>
            {s.saving ? "Saving…" : "Save"}
          </button>
          <Link class="button quiet" href={list}>
            Cancel
          </Link>
        </div>
      </form>
    </main>
  )
}

/** Create (`agentId` null) or edit an agent. Waits for the list: the form needs the stored agent and the providers. */
export function AgentForm({ model, serverId, agentId }: { model: AgentsModel; serverId: string; agentId: string | null }) {
  const { agents, loaded, providersLoaded, error } = useModel(model)
  const existing = agentId === null ? null : (agents.find((agent) => agent.id === agentId) ?? null)
  // The pickers need the providers for an edit too: a failed read shows its error instead of an empty form.
  const ready = loaded && providersLoaded && (agentId === null || existing !== null)
  const list = agentsPath(serverId)
  // One form for the visit: a later reading of the list does not replace what the user is typing.
  const form = useMemo(() => (ready ? model.form(existing) : null), [model, agentId, ready])
  if (form !== null) return <FormBody form={form} serverId={serverId} />
  const missing = loaded && providersLoaded && agentId !== null
  return (
    <main class="screen">
      <p>
        <Link href={list}>Agents</Link>
      </p>
      <h1>{agentId === null ? "New agent" : "Edit agent"}</h1>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : missing ? (
        <p class="problem">This agent no longer exists.</p>
      ) : (
        <p class="muted">Loading…</p>
      )}
    </main>
  )
}
