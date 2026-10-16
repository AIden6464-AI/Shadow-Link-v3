import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ShadowAgent, ShadowAlt, ShadowAnswer, ShadowGhost, ShadowStats, ShadowThread, ShadowWhy } from '../types'

// Shadow lives in a sidebar beside the conversation. After every Claude turn it
// asks the local Shadow Link daemon how you would likely reply, shows that as a
// ghost you can take with one key (and as dim text in the prompt box, Tab to
// take), explains it on request, and answers side questions about the session
// without touching the main conversation.

const PANE = 'shadow'

const ghostAtom = atom({ plugin: 'shadow-link', key: 'ghost' } as const, null as ShadowGhost | null)
const situationAtom = atom({ plugin: 'shadow-link', key: 'situation' } as const, '')
const whyAtom = atom({ plugin: 'shadow-link', key: 'why' } as const, null as ShadowWhy[] | null)
const whyOpenAtom = atom({ plugin: 'shadow-link', key: 'isWhyOpen' } as const, false)
const answerAtom = atom({ plugin: 'shadow-link', key: 'answer' } as const, null as ShadowAnswer | null)
const onlineAtom = atom({ plugin: 'shadow-link', key: 'isOnline' } as const, true)
const waitingAtom = atom({ plugin: 'shadow-link', key: 'isWaiting' } as const, false)
const statsAtom = atom({ plugin: 'shadow-link', key: 'stats' } as const, { corpus: null, learned: 0 } as ShadowStats)
const recentAtom = atom({ plugin: 'shadow-link', key: 'recent' } as const, [] as string[])
const draftingAtom = atom({ plugin: 'shadow-link', key: 'isDrafting' } as const, false)
const agentsAtom = atom({ plugin: 'shadow-link', key: 'agents' } as const, [] as ShadowAgent[])
const threadsAtom = atom({ plugin: 'shadow-link', key: 'threads' } as const, [] as ShadowThread[])
const openAgentAtom = atom({ plugin: 'shadow-link', key: 'openAgent' } as const, null as string | null)
const problemsAtom = atom({ plugin: 'shadow-link', key: 'problems' } as const, {} as Record<string, string>)

// The Shadow Link terminal palette (docs/brand.md, shadow_link/tui.py).
const THEME = {
  signal: '#5ff26d', // marks, actions, the ghost
  ok: '#74f288',
  warn: '#f4c95d',
  border: '#2fd94c',
  bar: '#07110b',
  ink: '#080d11',
  text: '#dde5e3',
  muted: '#a9bbb5',
}

const OFFLINE_STATUS = '⠇ shadow offline · run `shadow-link daemon`'

// Options, set by register() on each load.
let cfg = { baseUrl: 'http://localhost:5000', minConfidence: 0.08, suggestInPrompt: true, ghostModel: 'haiku' }

/** The option a model drafted, as opposed to a past reply Shadow retrieved. */
const DRAFTED = 'drafted'
// The sidebar opens itself once per load, and only where it docks beside the transcript.
let hasAutoOpened = false
// This session, so the AGENTS list shows the other agents' work, not its own.
let session = { id: '', cwd: '' }
let isDigesting = false
const AGENTS_REFRESH_MS = 15000

async function call($: EngineInterface, method: 'GET' | 'POST', path: string, body?: unknown): Promise<any | null> {
  let res
  try {
    res = await $.http.fetch(`${cfg.baseUrl.replace(/\/$/, '')}${path}`, {
      method,
      headers: method === 'POST' ? { 'Content-Type': 'application/json' } : {},
      body: method === 'POST' ? JSON.stringify(body) : undefined,
    })
  } catch {
    await update($, onlineAtom, () => false)
    $.ui.status(OFFLINE_STATUS)
    return null
  }
  await update($, onlineAtom, () => true)
  $.ui.status(undefined)
  if (!res.ok) return null
  try {
    return res.text ? JSON.parse(res.text) : {}
  } catch {
    return null // reachable, but not an answer we can read
  }
}

const post = ($: EngineInterface, path: string, body: unknown) => call($, 'POST', path, body)

const current = (g: ShadowGhost | null): ShadowAlt | null =>
  g && g.options.length ? g.options[g.index % g.options.length] ?? null : null

const clip = (text: string, max: number) =>
  text.length <= max ? text : `${text.slice(0, Math.max(0, max - 1))}…`

const toAlt = (raw: any): ShadowAlt | null =>
  raw && typeof raw.text === 'string' && raw.text.trim()
    ? {
        id: typeof raw.id === 'number' ? raw.id : null,
        text: raw.text.trim(),
        confidence: Number(raw.confidence ?? raw.signal ?? 0),
        bucket: String(raw.bucket ?? 'reply'),
      }
    : null

// ---------------------------------------------------------------- actions
async function refreshStats($: EngineInterface) {
  const data = await call($, 'GET', '/stats')
  if (data && typeof data.total_prompts === 'number') {
    const bytes = typeof data.database_bytes === 'number' ? data.database_bytes : null
    await update($, statsAtom, s => ({ ...s, corpus: data.total_prompts, bytes }))
  }
}

async function clearTurn($: EngineInterface) {
  await update($, ghostAtom, () => null)
  await update($, whyAtom, () => null)
  await update($, whyOpenAtom, () => false)
}

const DRAFT_SYSTEM = [
  'You predict the exact next message one specific person will type to their AI coding agent.',
  'Write only that message: no quotes, no preamble, no explanation.',
  'Match their voice exactly: length, casing, spelling, slang and punctuation, as their examples show.',
  'Most of their messages are short. If the agent asked a question or offered options, answer it the way they would.',
].join(' ')

function draftPrompt(situation: string, examples: { situation: string; reply: string }[], recent: string[]) {
  const shown = examples
    .filter(x => x.reply.trim())
    .slice(0, 8)
    .map((x, i) => `${i + 1}. Agent: "${clip(x.situation.replace(/\s+/g, ' '), 240)}"\n   They replied: "${clip(x.reply, 300)}"`)
    .join('\n')
  const said = situation.length > 3000 ? `…${situation.slice(-3000)}` : situation
  return [
    shown ? `How this person replied to their agent before:\n${shown}` : '',
    recent.length ? `Their latest messages in this session, oldest first:\n${recent.map(r => `- ${clip(r, 300)}`).join('\n')}` : '',
    `The agent (Claude) just said:\n"""\n${said}\n"""`,
    'Write their next message.',
  ].filter(Boolean).join('\n\n')
}

async function draft($: EngineInterface, situation: string, examples: any[]): Promise<string | null> {
  const recent = await read($, recentAtom)
  try {
    const r = await $.model.complete({
      model: cfg.ghostModel,
      system: DRAFT_SYSTEM,
      prompt: draftPrompt(situation, Array.isArray(examples) ? examples : [], recent),
      maxTokens: 120,
      timeoutMs: 20000,
    })
    if (!r.isAnswered) return null
    const text = r.text.trim().replace(/^["'“]+|["'”]+$/g, '').trim()
    return text || null
  } catch {
    return null
  }
}

async function predict($: EngineInterface, situation: string) {
  const useModel = cfg.ghostModel !== 'off'
  const data = await post($, '/predict', { question: situation, examples: useModel ? 8 : 0 })
  const retrieved = [data?.ghost, ...(data?.alternates ?? [])]
    .map(toAlt)
    .filter((a): a is ShadowAlt => a !== null && a.bucket !== 'generated' && a.confidence >= cfg.minConfidence)

  let options = retrieved
  if (useModel) {
    await update($, draftingAtom, () => true)
    await update($, ghostAtom, () => null)
    const text = await draft($, situation, data?.examples ?? [])
    await update($, draftingAtom, () => false)
    // A newer turn has started (or the person already sent): this draft is stale.
    if ((await read($, situationAtom)) !== situation || (await read($, waitingAtom))) return
    if (text) {
      options = [{ id: null, text, confidence: 1, bucket: DRAFTED }, ...retrieved.filter(a => a.text !== text)]
    }
  }

  const ghost: ShadowGhost | null = options.length ? { situation, options, index: 0 } : null
  await update($, ghostAtom, () => ghost)
  const top = current(ghost)
  if (top && cfg.suggestInPrompt) {
    await $.prompt.suggest({ text: top.text }).catch(() => undefined)
  }
}

/** One line on where a guess came from. */
const describe = (alt: ShadowAlt) =>
  alt.bucket === DRAFTED
    ? `drafted by ${cfg.ghostModel} in your voice`
    : `${alt.confidence >= 0.55 ? 'high' : alt.confidence >= 0.3 ? 'medium' : 'low'} · ${Math.round(alt.confidence * 100)}% · past reply`

async function useGhost($: EngineInterface, text: string) {
  // Put it in the chat bar to send (or edit first); feedback is learned when it is sent.
  await $.prompt.fill({ text, mode: 'replace' })
}

async function markWrong($: EngineInterface) {
  const g = await read($, ghostAtom)
  const alt = current(g)
  if (!g || !alt) return
  if (alt.id !== null) {
    await post($, '/feedback', { id: alt.id, vote: 'down', situation: g.situation })
  }
  const rest = g.options.filter((_, i) => i !== g.index % g.options.length)
  await update($, ghostAtom, () => (rest.length ? { ...g, options: rest, index: 0 } : null))
  await update($, whyOpenAtom, () => false)
  $.ui.toast('Shadow will weigh that reply less here')
}

async function explain($: EngineInterface) {
  const g = await read($, ghostAtom)
  if (!g) return
  if (await read($, whyOpenAtom)) {
    await update($, whyOpenAtom, () => false)
    return
  }
  await update($, whyOpenAtom, () => true)
  await update($, whyAtom, () => null)
  const data = await post($, '/aca/event', {
    text: g.situation,
    kind: 'observation',
    task_id: 'claude-code-shadow',
    remember: false,
  })
  const items: ShadowWhy[] = (data?.context ?? []).slice(0, 4).map((i: any) => ({
    title: String(i.title ?? ''),
    type: String(i.type ?? ''),
    reason: String(i.why?.seed_reason ?? ''),
  }))
  await update($, whyAtom, () => items)
}

async function ask($: EngineInterface, question: string) {
  const q = question.trim()
  if (!q) return
  await update($, answerAtom, () => ({ question: q, text: '', isPending: true }))

  // The user's own history, from Shadow Link's memory, rides along with the question.
  const memory = await post($, '/aca/event', {
    text: q,
    kind: 'prompt',
    task_id: 'claude-code-shadow',
    remember: false,
  })
  const recalled = (memory?.context ?? [])
    .slice(0, 6)
    .map((i: any) =>
      i.type === 'reaction' && i.situation
        ? `- When Claude said "${String(i.situation).slice(0, 200)}", the user replied "${String(i.content ?? '').slice(0, 200)}"`
        : `- (${i.type}) ${String(i.content ?? '').slice(0, 300)}`)
    .join('\n')

  const agents = await read($, agentsAtom)
  const others = agentsContext(agents)
  const timelines = await namedTimelines($, q, agents)

  const prompt = [
    'This is a side question from the user to Shadow, a separate assistant watching this session.',
    'Answer as Shadow, not as Claude: refer to Claude in the third person ("Claude tried…"),',
    'address the user as "you", and use at most 4 short sentences. Do not continue the task',
    'and do not call tools.',
    recalled ? `\nWhat Shadow Link remembers about this user:\n${recalled}` : '',
    others ? `\nWhat Shadow has seen the user's other coding agents doing:\n${others}` : '',
    timelines ? `\nDetail for the sessions this question is about:\n${timelines}` : '',
    `\nQuestion: ${q}`,
  ].join('\n')

  // A fork reads the session's own transcript (served from the prompt cache)
  // without adding anything to it.
  let text: string
  try {
    const r = await $.model.fork({ prompt })
    text = r.isAnswered
      ? r.text.trim()
      : r.reason === 'nothing-to-fork'
        ? 'Nothing to go on yet: ask again after Claude has replied once.'
        : `Shadow could not answer (${r.reason}).`
  } catch {
    text = 'Shadow could not answer (error).'
  }
  await update($, answerAtom, () => ({ question: q, text, isPending: false }))
}

// ------------------------------------------------------------ other agents
const ago = (ts: number) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  return s < 60 ? 'now' : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h` : `${Math.floor(s / 86400)}d`
}

const toAgent = (s: any): ShadowAgent => ({
  id: String(s.id),
  label: String(s.label ?? s.agent ?? 'agent'),
  projectName: String(s.project_name ?? ''),
  state: s.state === 'active' || s.state === 'idle' ? s.state : 'ended',
  doing: String(s.doing ?? ''),
  goal: String(s.goal ?? ''),
  openThread: String(s.open_thread ?? ''),
  last: Number(s.last ?? 0),
  digest: String(s.digest ?? ''),
  needsDigest: Boolean(s.needs_digest),
})

async function refreshAgents($: EngineInterface) {
  const q = session.id ? `&exclude=${encodeURIComponent(session.id)}` : ''
  const data = await call($, 'GET', `/sessions?limit=8${q}`)
  if (!data || !Array.isArray(data.sessions)) return
  const agents = data.sessions.map(toAgent)
  await update($, agentsAtom, () => agents)
  const problems = data.problems && typeof data.problems === 'object' ? data.problems : {}
  await update($, problemsAtom, () => Object.fromEntries(Object.entries(problems).map(([k, v]) => [k, String(v)])))
  const t = await call($, 'GET', `/threads?project=${encodeURIComponent(session.cwd)}`)
  if (t && Array.isArray(t.threads)) {
    await update($, threadsAtom, () => t.threads
      .filter((x: any) => x.session && !agents.every((a: ShadowAgent) => a.id !== x.session))
      .slice(0, 4)
      .map((x: any) => ({ session: String(x.session), agent: String(x.agent), projectName: String(x.project_name ?? ''), text: String(x.text ?? '') })))
  }
  await digestOne($, agents).catch(() => undefined)
}

const DIGEST_SYSTEM = [
  'You summarise one coding-agent session for the developer who ran it.',
  'Answer in exactly this shape:',
  'SUMMARY: 2 or 3 short plain sentences on what was being worked on, where it stands now, and what is left or blocked.',
  'DECISIONS: one line per decision the developer made (a choice, a rule, a direction), each starting "- ", written so it',
  'makes sense on its own (e.g. "- Use Haiku to draft replies, not a local model"). Write "DECISIONS: none" if there were none.',
  'Only list what the developer decided, never what the agent proposed and was not confirmed.',
].join('\n')

/** Split the model's digest into the summary and the decisions it found. */
function parseDigest(raw: string): { summary: string; decisions: string[] } {
  const text = raw.trim().replace(/^["'“]+|["'”]+$/g, '').trim()
  const [head, tail = ''] = text.split(/^\s*DECISIONS:/im)
  const summary = head.replace(/^\s*SUMMARY:\s*/i, '').trim()
  const decisions = /^\s*none\b/i.test(tail)
    ? []
    : tail.split('\n').map(l => l.trim()).filter(l => l.startsWith('-')).map(l => l.replace(/^-\s*/, '').trim()).filter(Boolean)
  return { summary: summary || text.trim(), decisions: decisions.slice(0, 8) }
}

/** Quiet sessions get a model-written summary in place of the template (one at a time). */
async function digestOne($: EngineInterface, agents: ShadowAgent[]) {
  if (isDigesting || cfg.ghostModel === 'off') return
  const target = agents.find(a => a.needsDigest && a.state === 'idle')
  if (!target) return
  isDigesting = true
  try {
    const detail = await call($, 'GET', `/sessions/${encodeURIComponent(target.id)}`)
    const rec = detail?.record
    if (!rec) return
    const files = Object.keys(rec.files ?? {}).slice(-12).join(', ')
    const cmds = (rec.commands ?? []).slice(-6).map((c: any) => `${c.cmd}${c.status === 'error' ? ' (failed)' : ''}`).join('\n')
    const prompt = [
      `Agent: ${target.label}. Project: ${target.projectName}.`,
      `Goal: ${rec.goal}`,
      rec.requests?.length ? `Requests, oldest first:\n${rec.requests.map((r: string) => `- ${clip(r, 200)}`).join('\n')}` : '',
      files ? `Files changed: ${files}` : '',
      cmds ? `Recent commands:\n${cmds}` : '',
      rec.errors?.length ? `Errors: ${rec.errors.map((e: any) => e.what).join('; ')}` : '',
      rec.last_agent ? `The agent's last reply:\n${clip(rec.last_agent, 800)}` : '',
      rec.open_thread ? `It is waiting on: ${rec.open_thread}` : '',
    ].filter(Boolean).join('\n\n')
    const r = await $.model.complete({ model: cfg.ghostModel, system: DIGEST_SYSTEM, prompt, maxTokens: 320, timeoutMs: 20000 })
    if (r.isAnswered && r.text.trim()) {
      const { summary, decisions } = parseDigest(r.text)
      await post($, `/sessions/${encodeURIComponent(target.id)}/digest`, { text: summary, by: cfg.ghostModel, decisions })
      await update($, agentsAtom, list => list.map(a => (a.id === target.id ? { ...a, digest: summary, needsDigest: false } : a)))
    }
  } finally {
    isDigesting = false
  }
}

/** Build a handoff brief from another agent's session and put it in the chat bar to send. */
async function handOver($: EngineInterface, sessionId: string): Promise<boolean> {
  const packet = await post($, '/handoff', { session: sessionId, to_agent: 'claude-code' })
  if (!packet || typeof packet.markdown !== 'string') {
    $.ui.toast('Shadow could not build that handoff')
    return false
  }
  await useGhost($, packet.markdown)
  $.ui.toast(`Handoff from ${packet.from_label ?? 'the agent'} is in the chat bar: send it to continue`)
  return true
}

/** The full timeline of up to two sessions the question names (by agent or project). */
async function namedTimelines($: EngineInterface, question: string, agents: ShadowAgent[]) {
  const q = question.toLowerCase()
  const named = agents.filter(a =>
    q.includes(a.label.toLowerCase()) || (a.projectName && q.includes(a.projectName.toLowerCase())))
  const parts: string[] = []
  for (const a of named.slice(0, 2)) {
    const d = await call($, 'GET', `/sessions/${encodeURIComponent(a.id)}`)
    const rec = d?.record
    if (!rec) continue
    const files = Object.entries(rec.files ?? {})
      .sort((x: any, y: any) => y[1].last - x[1].last).slice(0, 10)
      .map(([f, i]: any) => `${f} (${i.edits}x)`)
    parts.push([
      `${a.label} in ${a.projectName} (${a.state}, last active ${ago(a.last)} ago)`,
      `  goal: ${clip(rec.goal ?? '', 200)}`,
      rec.requests?.length ? `  requests: ${rec.requests.map((r: string) => clip(r, 140)).join(' | ')}` : '',
      files.length ? `  files changed: ${files.join(', ')}` : '',
      rec.commands?.length ? `  commands: ${rec.commands.slice(-6).map((c: any) => `${clip(c.cmd, 80)}${c.status === 'error' ? ' (failed)' : ''}`).join(' | ')}` : '',
      rec.tests?.length ? `  last tests: ${rec.tests[rec.tests.length - 1].status === 'ok' ? 'passed' : 'failed'}` : '',
      rec.errors?.length ? `  errors: ${rec.errors.map((e: any) => clip(e.what, 80)).join(' | ')}` : '',
      rec.decisions?.length ? `  decisions: ${rec.decisions.map((x: any) => clip(x.text, 120)).join(' | ')}` : '',
      rec.digest ? `  summary: ${clip(rec.digest, 400)}` : '',
      rec.open_thread ? `  waiting on: ${rec.open_thread}` : '',
    ].filter(Boolean).join('\n'))
  }
  return parts.join('\n')
}

/** What Shadow has seen the other agents doing, for side questions. */
function agentsContext(agents: ShadowAgent[]) {
  return agents.slice(0, 6).map(a =>
    `- ${a.label} in ${a.projectName || 'an unknown folder'} (${a.state}, ${ago(a.last)} ago): ${a.goal ? `goal "${clip(a.goal, 120)}"` : 'no goal recorded'}` +
    `${a.doing ? `; last ${a.doing}` : ''}${a.digest ? `; ${clip(a.digest.replace(/\s+/g, ' '), 240)}` : ''}${a.openThread ? `; waiting on "${clip(a.openThread, 120)}"` : ''}`,
  ).join('\n')
}

const openPane = ($: EngineInterface) => $.ui.open({ id: PANE, title: 'Shadow', columns: 38 })

export const register: Register = (on, options) => {
  cfg = {
    baseUrl: String(options.daemon_url ?? 'http://localhost:5000'),
    minConfidence: Number(options.min_confidence ?? 0.08),
    suggestInPrompt: options.suggest_in_prompt !== false,
    ghostModel: String(options.ghost_model ?? 'haiku').trim() || 'haiku',
  }
  hasAutoOpened = false

  // ----------------------------------------------------------------- events
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'shadow',
      description: 'Open the Shadow sidebar, `/shadow <question>` to ask, `/shadow handoff <codex|antigravity|session id>` to continue another agent\'s work, `/shadow status`.',
    })
    void refreshStats($).catch(() => undefined)
    session = { id: await $.session.id().catch(() => ''), cwd: e.cwd }
    void refreshAgents($).catch(() => undefined)
    $.clock.every(AGENTS_REFRESH_MS, () => refreshAgents($).catch(() => undefined))
    return next(e)
  })

  on('command.run', { command: 'shadow' }, async ($, e) => {
    const args = e.args.trim()
    if (args === 'status') {
      const online = await read($, onlineAtom)
      return { text: online ? `Shadow is linked to ${cfg.baseUrl}.` : `Shadow can't reach ${cfg.baseUrl}. Run: shadow-link daemon` }
    }
    const handoff = /^handoff\b\s*(.*)$/i.exec(args)
    if (handoff) {
      await refreshAgents($)
      const want = handoff[1].trim().toLowerCase()
      const agents = await read($, agentsAtom)
      const pick = agents.find(a => a.id.toLowerCase() === want)
        ?? agents.find(a => want && (a.label.toLowerCase().includes(want) || a.id.includes(want)))
        ?? (want ? undefined : agents[0])
      if (!pick) {
        return { text: want ? `Shadow has no recent session matching "${want}". Run /shadow to see the AGENTS list.` : 'Shadow has not seen another agent working yet.' }
      }
      const ok = await handOver($, pick.id)
      return { text: ok ? `Handoff from ${pick.label} (${pick.projectName}) is in the chat bar.` : 'Shadow could not build that handoff.' }
    }
    await openPane($)
    if (!args || args === 'open') return { text: 'Shadow sidebar open.' }
    await ask($, args)
    // A command's output row is read by the model too: keep the side answer in
    // the sidebar and out of the main conversation.
    return { text: 'Shadow answered in its sidebar.' }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // Main conversation only: subagent runs get no ghost.
    if (e.agentId === undefined) {
      await update($, waitingAtom, () => false)
      if (!e.isAborted && e.answer.trim()) {
        await clearTurn($)
        await update($, situationAtom, () => e.answer)
        await predict($, e.answer)
      }
      // After the ghost, so the guess never waits on the agents list.
      await refreshAgents($).catch(() => undefined)
    }
    return result
  })

  // Claude Code's own next-prompt guess gives way to Shadow's when Shadow has one.
  on('prompt.suggest', async ($, e, next) => {
    if (cfg.suggestInPrompt && e.origin.kind === 'suggestion') {
      const alt = current(await read($, ghostAtom))
      if (alt) return next({ ...e, text: alt.text })
    }
    return next(e)
  })

  // Learn from what the person actually sends.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer') {
      const text = e.text.trim()
      const situation = await read($, situationAtom)
      const g = await read($, ghostAtom)
      if (text && !text.startsWith('/')) {
        await update($, recentAtom, r => [...r, text].slice(-6))
      }
      if (text && situation && !text.startsWith('/')) {
        const stored = await post($, '/ingest', { question: situation, response: text, request_id: `claude-code-${Date.now()}` })
        if (typeof stored?.prompt_id === 'number') await update($, statsAtom, s => ({ corpus: s.corpus, learned: s.learned + 1 }))
        const sent = g?.options.find(o => o.text === text)
        if (sent && sent.id !== null) {
          await post($, '/feedback', { id: sent.id, vote: 'up', situation })
        }
        await refreshStats($)
      }
      await clearTurn($)
      await update($, waitingAtom, () => true)
    }
    return next(e)
  }).catch(($, e, next) => next(e)) // learning must never hold up a prompt

  // Fullscreen: open the sidebar once, beside the transcript. Main screen: no
  // room for a sidebar, so a two-row strip above the chat bar carries the ghost
  // and `more` opens the full panel inline.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.viewport?.isFullscreen === true) {
      if (!hasAutoOpened) {
        hasAutoOpened = true
        void openPane($).catch(() => undefined)
      }
      return next(e)
    }
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    if (e.props.hasSurvey || e.props.isWorking) return next(e)
    if ((await $.ui.panes()).some(p => p.id === PANE && p.isPlaced)) return next(e)

    const ghost = await read($, ghostAtom)
    const alt = current(ghost)
    if (!alt || !ghost) return next(e)

    const { Box, Text, Button } = $.ui.resolve(e)
    const meta = alt.bucket === DRAFTED ? 'drafted' : `${Math.round(alt.confidence * 100)}% past reply`
    const room = Math.max(10, e.props.bodyColumns - meta.length - 14)
    return (
      <Box flexDirection="column">
        <Box key="line" flexDirection="row" columnGap={1}>
          <Text key="mark" backgroundColor={THEME.signal} color={THEME.ink} bold> ⠇ SHADOW </Text>
          <Text key="meta" color={THEME.muted}>{meta}</Text>
          <Text key="ghost" color={THEME.signal} bold wrap="truncate-end">{clip(alt.text, room)}</Text>
        </Box>
        <Box key="actions" flexDirection="row" columnGap={1}>
          <Button key="use" hotkey="u" variant="primary" label="use" onPress={() => useGhost($, alt.text)} />
          {ghost.options.length > 1 ? (
            <Button key="next" hotkey="n" label="next"
              onPress={() => update($, ghostAtom, g => (g ? { ...g, index: g.index + 1 } : g))} />
          ) : null}
          <Button key="wrong" hotkey="x" label="wrong" onPress={() => markWrong($)} />
          <Button key="more" hotkey="m" label="more" onPress={() => openPane($)} />
        </Box>
      </Box>
    )
  })

  // ------------------------------------------------------------ the sidebar
  // Drawn in the Shadow Link terminal theme (docs/brand.md): signal green on
  // near black, a header bar, the GHOST box, and CORE MEMORY at the foot.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const width = Math.max(16, e.props.bodyColumns)
    const isDocked = e.props.placement === 'dock'

    const ghost = await read($, ghostAtom)
    const alt = current(ghost)
    const isWhyOpen = await read($, whyOpenAtom)
    const why = await read($, whyAtom)
    const answer = await read($, answerAtom)
    const online = await read($, onlineAtom)
    const waiting = await read($, waitingAtom)
    const drafting = await read($, draftingAtom)
    const stats = await read($, statsAtom)

    const section = (key: string, label: string) => (
      <Text key={key} color={THEME.signal} bold>{label}</Text>
    )

    const header = (
      <Box key="header" flexDirection="column">
        <Box key="bar" flexDirection="row" columnGap={1}>
          <Text key="name" backgroundColor={THEME.signal} color={THEME.ink} bold> ⠇ SHADOW LINK </Text>
          {online ? (
            <Text key="link" backgroundColor={THEME.bar} color={THEME.ok} bold> link:{waiting ? 'WATCHING' : 'MIRRORING'} </Text>
          ) : (
            <Text key="link" backgroundColor={THEME.bar} color={THEME.warn} bold> link:OFFLINE </Text>
          )}
        </Box>
        {isDocked ? <Text key="tagline" color={THEME.muted}>THE SHADOW OF YOU.</Text> : null}
        {!online ? (
          <Text key="offline" color={THEME.warn} wrap="wrap">run `shadow-link daemon` to link</Text>
        ) : null}
      </Box>
    )

    const ghostBox = (
      <Box key="ghost-box" flexDirection="column" borderStyle="round" borderColor={THEME.border} paddingX={1}>
        <Text key="ghost-title" color={THEME.signal} bold>GHOST</Text>
        {alt && ghost ? (
          <Box key="ghost-body" flexDirection="column">
            <Text key="meta" color={alt.bucket === DRAFTED || alt.confidence >= 0.55 ? THEME.ok : THEME.warn}>
              ⠇ {describe(alt)}
              {ghost.options.length > 1 ? ` · ${(ghost.index % ghost.options.length) + 1}/${ghost.options.length}` : ''}
            </Text>
            <Text key="ghost" color={THEME.signal} bold wrap="wrap">{alt.text}</Text>
          </Box>
        ) : (
          <Box key="ghost-body" flexDirection="column">
            <Text key="ghost-none" color={THEME.warn}>{drafting ? '⠇ drafting…' : '⠇ (waiting · no ghost yet)'}</Text>
            <Text key="ghost-hint" color={THEME.muted} wrap="wrap">
              {drafting
                ? `${cfg.ghostModel} is writing your likely reply`
                : waiting
                ? 'watching Claude…'
                : stats.corpus !== null && stats.corpus < 20
                  ? 'nothing yet: Shadow learns from each reply you send'
                  : 'no strong guess for this one'}
            </Text>
          </Box>
        )}
      </Box>
    )

    const ghostKeys = alt && ghost ? (
      <Box key="ghost-keys" flexDirection="row" flexWrap="wrap" columnGap={1}>
        <Button key="use" hotkey="u" variant="primary" label="use" onPress={() => useGhost($, alt.text)} />
        {ghost.options.length > 1 ? (
          <Button key="next" hotkey="n" label="next"
            onPress={() => update($, ghostAtom, g => (g ? { ...g, index: g.index + 1 } : g))} />
        ) : null}
        <Button key="why" hotkey="w" label="why" onPress={() => explain($)} />
        <Button key="wrong" hotkey="x" label="wrong" onPress={() => markWrong($)} />
      </Box>
    ) : null

    const whySection = alt && isWhyOpen ? (
      <Box key="why-list" flexDirection="column">
        {section('why-head', 'WHY')}
        {why === null ? (
          <Text key="why-wait" color={THEME.muted}>  looking…</Text>
        ) : why.length === 0 ? (
          <Text key="why-none" color={THEME.muted}>  only a word match with a past reply</Text>
        ) : (
          why.map((w, i) => (
            <Text key={`why-${i}`} wrap="wrap" color={THEME.text}>
              {'  '}• {clip(w.title, width * 2)} <Text color={THEME.muted}>({w.type}{w.reason ? `, ${w.reason}` : ''})</Text>
            </Text>
          ))
        )}
      </Box>
    ) : null

    const agents = await read($, agentsAtom)
    const threads = await read($, threadsAtom)
    const openAgent = await read($, openAgentAtom)
    const dot = (s: ShadowAgent['state']) => (s === 'active' ? THEME.signal : s === 'idle' ? THEME.warn : THEME.muted)
    // What agents are doing now: current sessions first; ended ones (over a day
    // quiet) only as a short "earlier" list when nothing is current.
    const liveAgents = agents.filter(a => a.state !== 'ended')
    const isEarlier = liveAgents.length === 0
    const shown = (isEarlier ? agents.slice(0, 3) : liveAgents).slice(0, isDocked ? 5 : 2)
    // A raw command says little about the work; the file being edited or the goal says more.
    const summaryLine = (a: ShadowAgent) =>
      a.doing.startsWith('editing ') ? a.doing : a.goal ? `goal: ${a.goal}` : a.doing
    const problems = Object.entries(await read($, problemsAtom))
    const agentsSection = online ? (
      <Box key="agents" flexDirection="column">
        {section('agents-head', 'AGENTS')}
        {problems.map(([agent, why], i) => (
          <Text key={`problem-${i}`} color={THEME.warn} wrap="wrap">{'  '}can't read {agent}: {clip(why, 120)}</Text>
        ))}
        {shown.length === 0 ? (
          <Text key="agents-none" color={THEME.muted} wrap="wrap">No other agent sessions yet. Codex and Antigravity show up here as they work.</Text>
        ) : null}
        {isEarlier && shown.length ? (
          <Text key="agents-earlier" color={THEME.muted}>{'  '}nothing running now · earlier:</Text>
        ) : null}
        {shown.length === 0 ? null : (
          shown.map((a, i) => (
            <Box key={`agent-${i}`} flexDirection="column">
              <Text key="row" wrap="truncate-end">
                <Text color={dot(a.state)}>●</Text> <Text color={THEME.text} bold>{a.label}</Text>
                <Text color={THEME.muted}> · {a.projectName} · {ago(a.last)}</Text>
              </Text>
              <Text key="doing" color={THEME.muted} wrap="truncate-end">{'  '}{clip(summaryLine(a), width * 2)}</Text>
              {openAgent === a.id ? (
                <Box key="detail" flexDirection="column">
                  {a.goal && summaryLine(a) !== `goal: ${a.goal}` ? <Text key="goal" color={THEME.text} wrap="wrap">{'  '}goal: {clip(a.goal, 200)}</Text> : null}
                  {a.doing && a.doing !== summaryLine(a) ? <Text key="last" color={THEME.muted} wrap="wrap">{'  '}last: {clip(a.doing, 120)}</Text> : null}
                  {a.digest ? <Text key="digest" color={THEME.text} wrap="wrap">{'  '}{clip(a.digest, 500)}</Text> : null}
                  {a.openThread ? <Text key="waiting" color={THEME.warn} wrap="wrap">{'  '}waiting on: {a.openThread}</Text> : null}
                </Box>
              ) : null}
              <Box key="acts" flexDirection="row" columnGap={1}>
                <Text key="pad">{' '}</Text>
                <Button key={`open-${i}`} label={openAgent === a.id ? 'less' : 'open'}
                  onPress={() => update($, openAgentAtom, cur => (cur === a.id ? null : a.id))} />
                <Button key={`handover-${i}`} label="hand over" onPress={() => handOver($, a.id)} />
              </Box>
            </Box>
          ))
        )}
      </Box>
    ) : null

    const threadsSection = isDocked && threads.length ? (
      <Box key="threads" flexDirection="column">
        {section('threads-head', 'OPEN THREADS')}
        {threads.map((t, i) => (
          <Text key={`thread-${i}`} color={THEME.text} wrap="wrap">
            {'  '}• {clip(t.text, 160)} <Text color={THEME.muted}>({t.agent})</Text>
          </Text>
        ))}
      </Box>
    ) : null

    const askSection = (
      <Box key="ask-section" flexDirection="column">
        {section('ask-head', 'ASK SHADOW')}
        {answer ? (
          <Box key="answer" flexDirection="column">
            <Text key="q" color={THEME.muted} wrap="wrap">you › {answer.question}</Text>
            <Text key="a" color={THEME.text} wrap="wrap">{answer.isPending ? 'thinking…' : answer.text}</Text>
            {!answer.isPending ? (
              <Box key="answer-actions" flexDirection="row" columnGap={1}>
                <Button key="send-answer" hotkey="s" label="send to Claude" onPress={() => useGhost($, answer.text)} />
                <Button key="clear-answer" hotkey="c" label="clear" onPress={() => update($, answerAtom, () => null)} />
              </Box>
            ) : null}
          </Box>
        ) : (
          <Text key="ask-hint" color={THEME.muted} wrap="wrap">Claude never sees these.</Text>
        )}
        <Input key="ask" placeholder="ask about this session…" submitLabel="ask" onSubmit={value => ask($, value)} />
      </Box>
    )

    const memory = (
      <Box key="memory" flexDirection="column">
        {isDocked ? section('memory-head', 'CORE MEMORY') : null}
        <Text key="memory-corpus" color={THEME.text}>
          {isDocked ? '  ' : ''}{stats.corpus !== null ? stats.corpus.toLocaleString() : '—'} <Text color={THEME.muted}>replies learned</Text>
        </Text>
        {stats.learned > 0 ? (
          <Text key="memory-session" color={THEME.text}>
            {isDocked ? '  ' : ''}{stats.learned} <Text color={THEME.muted}>this session</Text>
          </Text>
        ) : null}
        {isDocked && stats.bytes ? (
          <Text key="memory-size" color={THEME.text}>
            {'  '}{(stats.bytes / 1048576).toFixed(1)} MB <Text color={THEME.muted}>on disk</Text>
          </Text>
        ) : null}
      </Box>
    )

    return (
      <Box flexDirection="column" rowGap={isDocked ? 1 : 0}>
        {header}
        <Box key="ghost-group" flexDirection="column">
          {ghostBox}
          {ghostKeys}
        </Box>
        {whySection}
        {agentsSection}
        {threadsSection}
        {askSection}
        {memory}
      </Box>
    )
  })
}
