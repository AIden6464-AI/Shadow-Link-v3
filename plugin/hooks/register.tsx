import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ShadowAlt, ShadowAnswer, ShadowGhost, ShadowStats, ShadowWhy } from '../types'

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
    await update($, statsAtom, s => ({ ...s, corpus: data.total_prompts }))
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

  const prompt = [
    'This is a side question from the user to Shadow, a separate assistant watching this session.',
    'Answer as Shadow, not as Claude: refer to Claude in the third person ("Claude tried…"),',
    'address the user as "you", and use at most 4 short sentences. Do not continue the task',
    'and do not call tools.',
    recalled ? `\nWhat Shadow Link remembers about this user:\n${recalled}` : '',
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
      description: 'Open the Shadow sidebar, `/shadow <question>` to ask about this session, `/shadow status` to check the daemon.',
    })
    void refreshStats($).catch(() => undefined)
    return next(e)
  })

  on('command.run', { command: 'shadow' }, async ($, e) => {
    const args = e.args.trim()
    if (args === 'status') {
      const online = await read($, onlineAtom)
      return { text: online ? `Shadow is linked to ${cfg.baseUrl}.` : `Shadow can't reach ${cfg.baseUrl}. Run: shadow-link daemon` }
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
        {askSection}
        {memory}
      </Box>
    )
  })
}
