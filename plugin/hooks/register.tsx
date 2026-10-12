import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ShadowAlt, ShadowAnswer, ShadowGhost, ShadowView, ShadowWhy } from '../types'

// Shadow floats above the chat bar. After every Claude turn it asks the local
// Shadow Link daemon how you would likely reply, shows that as a ghost you can
// send with one key (and as dim text in the prompt box, Tab to take), explains
// it on request, and answers side questions about the session without
// touching the main conversation.

const ghostAtom = atom({ plugin: 'shadow-link', key: 'ghost' } as const, null as ShadowGhost | null)
const situationAtom = atom({ plugin: 'shadow-link', key: 'situation' } as const, '')
const viewAtom = atom({ plugin: 'shadow-link', key: 'view' } as const, 'ghost' as ShadowView)
const whyAtom = atom({ plugin: 'shadow-link', key: 'why' } as const, null as ShadowWhy[] | null)
const answerAtom = atom({ plugin: 'shadow-link', key: 'answer' } as const, null as ShadowAnswer | null)
const onlineAtom = atom({ plugin: 'shadow-link', key: 'isOnline' } as const, true)
const hiddenAtom = atom({ plugin: 'shadow-link', key: 'isHidden' } as const, false)

const OFFLINE_STATUS = '◐ shadow offline · run `shadow-link daemon`'

// Options, set by register() on each load.
let cfg = { baseUrl: 'http://localhost:5000', minConfidence: 0.15, suggestInPrompt: true }

async function post($: EngineInterface, path: string, body: unknown): Promise<any | null> {
  try {
    const res = await $.http.fetch(`${cfg.baseUrl.replace(/\/$/, '')}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    await update($, onlineAtom, () => true)
    $.ui.status(undefined)
    if (!res.ok) return null
    return res.text ? JSON.parse(res.text) : {}
  } catch {
    await update($, onlineAtom, () => false)
    $.ui.status(OFFLINE_STATUS)
    return null
  }
}

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
async function clearTurn($: EngineInterface) {
  await update($, ghostAtom, () => null)
  await update($, whyAtom, () => null)
  await update($, answerAtom, () => null)
  await update($, viewAtom, () => 'ghost' as ShadowView)
}

async function predict($: EngineInterface, situation: string) {
  const data = await post($, '/predict', { question: situation })
  const all = [data?.ghost, ...(data?.alternates ?? [])]
    .map(toAlt)
    .filter((a): a is ShadowAlt => a !== null && a.confidence >= cfg.minConfidence)
  const ghost: ShadowGhost | null = all.length ? { situation, options: all, index: 0 } : null
  await update($, ghostAtom, () => ghost)
  const top = current(ghost)
  if (top && cfg.suggestInPrompt) {
    await $.prompt.suggest({ text: top.text }).catch(() => undefined)
  }
}

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
  $.ui.toast('Shadow will weigh that reply less here')
}

async function explain($: EngineInterface) {
  const g = await read($, ghostAtom)
  if (!g) return
  await update($, viewAtom, () => 'why' as ShadowView)
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
  await update($, viewAtom, () => 'answer' as ShadowView)
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
  const r = await $.model.fork({ prompt })
  const text = r.isAnswered
    ? r.text.trim()
    : r.reason === 'nothing-to-fork'
      ? 'Nothing to go on yet: ask again after Claude has replied once.'
      : `Shadow could not answer (${r.reason}).`
  await update($, answerAtom, () => ({ question: q, text, isPending: false }))
}


export const register: Register = (on, options) => {
  cfg = {
    baseUrl: String(options.daemon_url ?? 'http://localhost:5000'),
    minConfidence: Number(options.min_confidence ?? 0.15),
    suggestInPrompt: options.suggest_in_prompt !== false,
  }

  // ----------------------------------------------------------------- events
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'shadow',
      description: 'Shadow: show/hide the floating ghost, or `/shadow <question>` to ask about this session.',
    })
    return next(e)
  })

  on('command.run', { command: 'shadow' }, async ($, e) => {
    const args = e.args.trim()
    if (!args || args === 'toggle') {
      const hidden = await update($, hiddenAtom, h => !h)
      return { text: hidden ? 'Shadow hidden. /shadow to show it again.' : 'Shadow is back above the chat bar.' }
    }
    if (args === 'status') {
      const online = await read($, onlineAtom)
      return { text: online ? `Shadow is linked to ${cfg.baseUrl}.` : `Shadow can't reach ${cfg.baseUrl}. Run: shadow-link daemon` }
    }
    await update($, hiddenAtom, () => false)
    await ask($, args)
    // A command's output row is read by the model too: keep the side answer in
    // the band and out of the main conversation.
    return { text: 'Shadow answered above the chat bar.' }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // Main conversation only: subagent runs and interrupted turns get no ghost.
    if (e.agentId === undefined && !e.isAborted && e.answer.trim()) {
      await clearTurn($)
      await update($, situationAtom, () => e.answer)
      await predict($, e.answer)
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
    // `@shadow <question>` in the chat bar goes to Shadow, never to Claude.
    const toShadow = e.origin.kind === 'composer' ? /^@shadow\b\s*([\s\S]*)$/i.exec(e.text.trim()) : null
    if (toShadow) {
      const question = (toShadow[1] ?? '').trim()
      await update($, hiddenAtom, () => false)
      if (question) {
        $.clock.after(0, async () => {
          await $.prompt.fill({ text: '', mode: 'replace' }) // the question was for Shadow: clear the bar
          await ask($, question)
        })
      }
      return { drop: question ? 'Asked Shadow: the answer floats above the chat bar.' : 'Type @shadow followed by a question.' }
    }
    if (e.origin.kind === 'composer') {
      const text = e.text.trim()
      const situation = await read($, situationAtom)
      const g = await read($, ghostAtom)
      if (text && situation) {
        await post($, '/ingest', { question: situation, response: text, request_id: `claude-code-${Date.now()}` })
        const sent = g?.options.find(o => o.text === text)
        if (sent && sent.id !== null) {
          await post($, '/feedback', { id: sent.id, vote: 'up', situation })
        }
      }
      await clearTurn($)
    }
    return next(e)
  }).catch(($, e, next) => next(e)) // learning must never hold up a prompt

  // --------------------------------------------------------------- the band
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // The band is drawn on the terminal and desktop surfaces only.
    if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)
    if (e.props.hasSurvey || (await read($, hiddenAtom))) return next(e)

    const ghost = await read($, ghostAtom)
    const view = await read($, viewAtom)
    const answer = await read($, answerAtom)
    const why = await read($, whyAtom)
    const alt = current(ghost)
    const width = Math.max(20, e.props.bodyColumns)

    // Nothing to say, or Claude is still working on this turn: stay out of the way.
    if (e.props.isWorking || (!alt && !(view === 'answer' && answer))) return next(e)

    const { Box, Text, Button, Input } = $.ui.resolve(e)

    const header = (
      <Box key="head" flexDirection="row" gap={1}>
        <Text color="magenta" bold>◐ shadow</Text>
        {alt ? (
          <Text dimColor>
            {Math.round(alt.confidence * 100)}% · {alt.bucket}
            {ghost && ghost.options.length > 1 ? ` · ${(ghost.index % ghost.options.length) + 1}/${ghost.options.length}` : ''}
          </Text>
        ) : null}
      </Box>
    )

    const askBox = (
      <Input
        key="ask"
        placeholder="ask shadow… (or type @shadow <question> in the chat bar)"
        submitLabel="ask"
        onSubmit={value => ask($, value)}
      />
    )

    if (view === 'answer' && answer) {
      return (
        <Box flexDirection="column">
          {header}
          <Text key="q" dimColor wrap="truncate-end">you › {clip(answer.question, width - 6)}</Text>
          <Text key="a" wrap="wrap">{answer.isPending ? 'thinking…' : answer.text}</Text>
          <Box key="actions" flexDirection="row" gap={1}>
            {!answer.isPending ? (
              <Button key="send-answer" hotkey="s" label="send to Claude" onPress={() => useGhost($, answer.text)} />
            ) : null}
            {alt ? <Button key="back" hotkey="b" label="back" onPress={() => update($, viewAtom, () => 'ghost' as ShadowView)} /> : null}
            <Button key="close" hotkey="c" label="close" role="dismiss"
              onPress={async () => { await update($, answerAtom, () => null); await update($, viewAtom, () => 'ghost' as ShadowView) }} />
          </Box>
          {askBox}
        </Box>
      )
    }

    if (!alt || !ghost) return next(e)

    if (view === 'why') {
      return (
        <Box flexDirection="column">
          {header}
          <Text key="ghost" wrap="truncate-end">{clip(alt.text, width)}</Text>
          <Text key="why-head" dimColor>why: what this situation brought back</Text>
          {why === null ? (
            <Text key="why-wait" dimColor>  looking…</Text>
          ) : why.length === 0 ? (
            <Text key="why-none" dimColor>  only a word match with a past reply</Text>
          ) : (
            why.map((w, i) => (
              <Text key={`why-${i}`} wrap="truncate-end">
                {'  '}• {clip(w.title, width - 30)} <Text dimColor>({w.type}{w.reason ? `, ${w.reason}` : ''})</Text>
              </Text>
            ))
          )}
          <Box key="actions" flexDirection="row" gap={1}>
            <Button key="use" hotkey="u" variant="primary" label="use" onPress={() => useGhost($, alt.text)} />
            <Button key="back" hotkey="b" label="back" onPress={() => update($, viewAtom, () => 'ghost' as ShadowView)} />
          </Box>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {header}
        <Text key="ghost" wrap="wrap">{alt.text}</Text>
        <Box key="actions" flexDirection="row" gap={1}>
          <Button key="use" hotkey="u" variant="primary" label="use" onPress={() => useGhost($, alt.text)} />
          {ghost.options.length > 1 ? (
            <Button key="next" hotkey="n" label="next"
              onPress={() => update($, ghostAtom, g => (g ? { ...g, index: g.index + 1 } : g))} />
          ) : null}
          <Button key="why" hotkey="w" label="why" onPress={() => explain($)} />
          <Button key="wrong" hotkey="x" label="wrong" onPress={() => markWrong($)} />
          <Button key="hide" hotkey="h" label="hide" role="dismiss" onPress={() => update($, hiddenAtom, () => true)} />
        </Box>
        {askBox}
      </Box>
    )
  })
}
