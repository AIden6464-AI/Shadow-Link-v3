import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const SITUATION = "I'll set up a Docker container for the dev environment."
const GHOST = "no, don't use docker. use the venv"
const ALT = 'keep it short, bullet points'
const DRAFT = 'nah keep the venv, docker is overkill here'

type Calls = {
  fetches: { url: string; method: string; body: any }[]
  suggested: string[]
  filled: string[]
  forks: string[]
  opened: string[]
  status: (string | undefined)[]
  drafts: { model: string; prompt: string; system?: string }[]
}

/** What Claude Code itself draws above the prompt. */
const ENGINE_BAND = 'claude-code-default-band'

/** A fake Shadow Link daemon and model beneath the plugin. */
const CODEX = {
  id: 'S-codex-abc123', agent: 'codex', label: 'Codex', project_name: 'veyr', project: 'D:\\veyr',
  state: 'idle', doing: 'editing chat.html', goal: 'make the admin page show model costs',
  open_thread: 'Want me to add the dropdown too?', last: Date.now() / 1000 - 3600,
  digest: 'Changed 3 file(s): chat.html, admin.py, styles.css', digest_by: 'template', needs_digest: true,
}
const ANTIGRAVITY = {
  id: 'S-antigravity-def456', agent: 'antigravity', label: 'Antigravity', project_name: 'fetcher-api',
  project: 'D:\\fetcher-api', state: 'active', doing: 'ran git status', goal: 'fix the fetch retries',
  open_thread: '', last: Date.now() / 1000 - 60, digest: '', digest_by: '', needs_digest: false,
}
const HANDOFF = '# Handoff from Codex: veyr\n\nGoal: make the admin page show model costs'

function fakes(on: On, opts: { isDown?: boolean; forkThrows?: boolean; badJson?: boolean; draft?: string; gate?: { started: () => void; open: Promise<void> }; sessions?: any[]; problems?: Record<string, string> } = {}): Calls {
  const calls: Calls = { fetches: [], suggested: [], filled: [], forks: [], opened: [], status: [], drafts: [] }
  // The ghost model: unavailable unless a test gives it a draft, so Shadow falls back to past replies.
  on('model.complete', async ($, e) => {
    calls.drafts.push({ model: e.model, prompt: String(e.prompt), system: e.system ? String(e.system) : undefined })
    if (!opts.draft) throw new Error('model unavailable')
    if (opts.gate) {
      opts.gate.started()
      await opts.gate.open
    }
    return {
      value: {
        isAnswered: true as const,
        text: `"${opts.draft}"`,
        usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      },
    }
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text key="engine">{ENGINE_BAND}</Text>
  })
  on('ui.open', ($, e) => {
    calls.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.status', ($, e) => {
    calls.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => ({ value: undefined }))
  on('command.register', ($, e) => ({ value: undefined }))
  on('ui.panes', () => ({
    value: calls.opened.map(id => ({ id, title: 'Shadow', isShown: true, isFocused: false, isPlaced: true })),
  }))
  on('http.fetch', ($, e) => {
    if (opts.isDown) return { deny: 'connection refused' }
    const body = e.init?.body ? JSON.parse(e.init.body) : null
    calls.fetches.push({ url: e.url, method: e.init?.method ?? 'GET', body })
    const path = new URL(e.url).pathname
    if (opts.badJson && path === '/predict') {
      return { value: { status: 200, ok: true, headers: {}, text: '<html>oops</html>' } }
    }
    const reply =
      path === '/predict'
        ? {
            ghost: { id: 1, text: GHOST, bucket: 'rejection', confidence: 0.68 },
            examples: [{ id: 1, situation: SITUATION, reply: GHOST }],
            alternates: [
              { id: 5, text: ALT, bucket: 'request', confidence: 0.3 },
              { id: 9, text: 'noise', bucket: 'request', confidence: 0.02 },
            ],
          }
        : path === '/aca/event'
          ? { context: [{ title: 'you rejected Docker for dev', type: 'reaction', content: 'use the venv', situation: SITUATION, why: { seed_reason: 'lexical match 0.55' } }] }
          : path === '/stats'
            ? { total_prompts: 1204, database_bytes: 3 * 1048576 }
            : path === '/ingest'
              ? { status: 'ingested', prompt_id: 42 }
              : path === '/sessions'
                ? { sessions: opts.sessions ?? [], problems: opts.problems ?? {} }
                : path === '/threads'
                  ? { threads: (opts.sessions ?? []).filter(s => s.open_thread).map(s => ({ session: s.id, agent: s.label, project_name: s.project_name, text: s.open_thread })) }
                  : path === '/handoff'
                    ? { id: 'H-1', markdown: HANDOFF, from_label: 'Codex' }
                    : path.startsWith('/sessions/') && !path.endsWith('/digest')
                      ? { record: { goal: CODEX.goal, requests: ['add model costs to admin'], files: { 'D:\\veyr\\chat.html': { edits: 2 } }, commands: [{ cmd: 'pytest', status: 'ok' }], errors: [], last_agent: 'Done. Want me to add the dropdown too?', open_thread: CODEX.open_thread } }
                      : {}
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(reply) } }
  })
  on('prompt.suggest', ($, e) => {
    calls.suggested.push(e.text)
    return { isShown: true }
  })
  on('prompt.fill', ($, e) => {
    calls.filled.push(e.text)
    return { isFilled: true }
  })
  on('model.fork', ($, e) => {
    calls.forks.push(e.prompt)
    if (opts.forkThrows) throw new Error('model unavailable')
    return {
      value: {
        isAnswered: true as const,
        text: 'Yes: docker compose for the database only.',
        usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      },
    }
  })
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('prompt.submit', ($, e) => ({ text: e.text }))
  return calls
}

async function claudeReplies($: Engine, answer = SITUATION, extra: { agentId?: string } = {}) {
  await $.turn.complete({ answer, durationMs: 1000, isAborted: false, reason: 'answer', turnId: 't1', ...extra })
}

function sidebar($: Engine, surface: 'terminal' | 'desktop') {
  return $.ui.mount({
    plugin: 'shadow-link',
    surface,
    component: 'Pane',
    requestId: 'shadow',
    props: { title: 'Shadow', isFocused: false, bodyColumns: 38, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
  })
}

function band($: Engine, isFullscreen: boolean, isWorking = false) {
  return $.ui.mount({
    plugin: 'shadow-link',
    surface: 'terminal',
    component: 'AbovePrompt',
    viewport: { columns: isFullscreen ? 160 : 95, rows: 50, isFullscreen },
    props: { hasSurvey: false, isWorking, maxRows: 12, bodyColumns: 120, scroll: { offset: 0, bodyRows: 12 }, view: {} },
  })
}

const paths = (c: Calls) => c.fetches.map(f => new URL(f.url).pathname)

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: a Claude turn puts the ghost in the sidebar and in the prompt box`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    expect(paths(calls)).toContain('/predict')
    expect(calls.suggested).toEqual([GHOST])

    const ui = await sidebar($, surface)
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /high · 68% · past reply · 1\/2/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: low-confidence guesses are not shown`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    expect(await ui.find({ type: 'Text', text: 'noise' })).toBeUndefined()
    await ui.press({ key: 'next' })
    expect(await ui.find({ type: 'Text', text: ALT })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: use puts the ghost in the chat bar; sending it teaches Shadow`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    await ui.press({ key: 'use' })
    expect(calls.filled).toEqual([GHOST])

    await $.prompt.submit({ text: GHOST, wait: false, origin: { kind: 'composer' } })
    const ingest = calls.fetches.find(f => f.url.endsWith('/ingest'))
    expect(ingest?.body).toMatchObject({ question: SITUATION, response: GHOST })
    const up = calls.fetches.find(f => f.url.endsWith('/feedback'))
    expect(up?.body).toMatchObject({ id: 1, vote: 'up' })
    // The turn is answered: the guess clears while Claude works, and the count goes up.
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'watching Claude…' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 this session/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: wrong down-votes the ghost and shows the next guess`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    await ui.press({ key: 'wrong' })
    const down = calls.fetches.find(f => f.url.endsWith('/feedback'))
    expect(down?.body).toMatchObject({ id: 1, vote: 'down' })
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: ALT })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: why shows what the situation brought back, and toggles off`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    await ui.press({ key: 'why' })
    expect(await ui.find({ type: 'Text', text: /you rejected Docker for dev/ })).toBeDefined()
    await ui.press({ key: 'why' })
    expect(await ui.find({ type: 'Text', text: /you rejected Docker for dev/ })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: a side question is answered over the session without touching it`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    await ui.input({ key: 'ask', text: 'did I ever decide on postgres?' })
    expect(calls.forks.length).toBe(1)
    expect(calls.forks[0]).toContain('did I ever decide on postgres?')
    expect(calls.forks[0]).toContain('use the venv') // recalled from Shadow Link's memory
    expect(calls.forks[0]).toContain(`When Claude said "${SITUATION}"`)
    expect(await ui.find({ type: 'Text', text: /docker compose for the database only/ })).toBeDefined()
    await ui.press({ key: 'send-answer' })
    expect(calls.filled).toEqual(['Yes: docker compose for the database only.'])
    await ui.unmount()
  })

  test(`${surface}: /shadow <question> opens the sidebar and answers there, not in the conversation`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await sidebar($, surface)
    const out = await $.command.run({ command: 'shadow', args: 'what did I decide about docker?', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
    expect(calls.opened).toContain('shadow')
    expect(calls.forks.length).toBe(1)
    expect(JSON.stringify(out)).not.toContain('docker compose for the database only')
    expect(await ui.find({ type: 'Text', text: /docker compose for the database only/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: a failing side question does not hang on "thinking…"`, async ($, on) => {
    fakes(on, { forkThrows: true })
    await claudeReplies($)
    const ui = await sidebar($, surface)
    await ui.input({ key: 'ask', text: 'what now?' })
    expect(await ui.find({ type: 'Text', text: 'thinking…' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /could not answer/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: the sidebar shows the link and corpus size`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    await $.prompt.submit({ text: 'use the venv please', wait: false, origin: { kind: 'composer' } })
    const ui = await sidebar($, surface)
    expect(await ui.find({ type: 'Text', text: /1,204 replies learned/ })).toBeDefined()
    await ui.unmount()
  })
}

test('the sidebar opens itself in fullscreen and stays out of the main screen', async ($, on) => {
  const calls = fakes(on)
  const main = await band($, false)
  expect(await main.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
  await main.unmount()
  expect(calls.opened).toEqual([])

  const full = await band($, true)
  expect(await full.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
  await full.unmount()
  expect(calls.opened).toEqual(['shadow'])
})

test('main screen: a two-row strip above the chat bar carries the ghost', async ($, on) => {
  const calls = fakes(on)
  await claudeReplies($)
  const ui = await band($, false)
  expect(await ui.find({ type: 'Text', text: GHOST })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '68% past reply' })).toBeDefined()
  await ui.press({ key: 'use' })
  expect(calls.filled).toEqual([GHOST])
  await ui.press({ key: 'more' })
  expect(calls.opened).toEqual(['shadow'])
  await ui.unmount()
})

test('main screen: the strip stays out of the way while Claude works or with nothing to say', async ($, on) => {
  fakes(on)
  const empty = await band($, false)
  expect(await empty.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
  await empty.unmount()
  await claudeReplies($)
  const working = await band($, false, true)
  expect(await working.find({ type: 'Text', text: GHOST })).toBeUndefined()
  expect(await working.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
  await working.unmount()
})

test('the ghost model drafts the reply in your voice, from your past replies and latest messages', async ($, on) => {
  const calls = fakes(on, { draft: DRAFT })
  await $.prompt.submit({ text: 'set up the dev env', wait: false, origin: { kind: 'composer' } })
  await claudeReplies($)
  expect(calls.drafts.length).toBe(1)
  expect(calls.drafts[0].model).toBe('haiku')
  expect(calls.drafts[0].prompt).toContain(`They replied: "${GHOST}"`) // a past reply, with its situation
  expect(calls.drafts[0].prompt).toContain('- set up the dev env') // what they just said
  expect(calls.drafts[0].prompt).toContain(SITUATION)
  const fetched = calls.fetches.find(f => f.url.endsWith('/predict'))
  expect(fetched?.body).toMatchObject({ examples: 8 })
  expect(calls.suggested).toEqual([DRAFT]) // quotes stripped, in the prompt box

  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: DRAFT })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /drafted by haiku in your voice · 1\/3/ })).toBeDefined()
  await ui.press({ key: 'next' })
  expect(await ui.find({ type: 'Text', text: GHOST })).toBeDefined() // the past replies follow
  await ui.unmount()
})

test('a draft that lands after the person already replied is dropped', async ($, on) => {
  let started = () => {}
  let release = () => {}
  const drafting = new Promise<void>(resolve => { started = resolve })
  const open = new Promise<void>(resolve => { release = resolve })
  const calls = fakes(on, { draft: DRAFT, gate: { started, open } })
  const turn = claudeReplies($)
  await drafting // the model is drafting…
  await $.prompt.submit({ text: 'never mind, I did it', wait: false, origin: { kind: 'composer' } })
  release()
  await turn
  expect(calls.suggested).toEqual([])
  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: DRAFT })).toBeUndefined()
  await ui.unmount()
})

test('AGENTS lists what the other agents are doing, and open shows the detail', async ($, on) => {
  fakes(on, { sessions: [ANTIGRAVITY, { ...CODEX, needs_digest: false }] })
  await claudeReplies($)
  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /Antigravity/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /goal: fix the fetch retries/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /editing chat\.html/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /goal: make the admin page/ })).toBeUndefined()
  await ui.press({ key: 'open-1' })
  expect(await ui.find({ type: 'Text', text: /goal: make the admin page/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /waiting on: Want me to add the dropdown too\?/ })).toBeDefined()
  // Its question shows under OPEN THREADS too.
  expect(await ui.find({ type: 'Text', text: 'OPEN THREADS' })).toBeDefined()
  await ui.unmount()
})

test('hand over puts the brief in the chat bar to send, never sends it', async ($, on) => {
  const calls = fakes(on, { sessions: [{ ...CODEX, needs_digest: false }] })
  await claudeReplies($)
  const ui = await sidebar($, 'terminal')
  await ui.press({ key: 'handover-0' })
  const req = calls.fetches.find(f => f.url.endsWith('/handoff'))
  expect(req?.body).toMatchObject({ session: CODEX.id, to_agent: 'claude-code' })
  expect(calls.filled).toEqual([HANDOFF])
  await ui.unmount()
})

test('/shadow handoff codex picks that agent\'s latest session', async ($, on) => {
  const calls = fakes(on, { sessions: [ANTIGRAVITY, { ...CODEX, needs_digest: false }] })
  const out = await $.command.run({ command: 'shadow', args: 'handoff codex', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  expect(calls.fetches.find(f => f.url.endsWith('/handoff'))?.body).toMatchObject({ session: CODEX.id })
  expect(calls.filled).toEqual([HANDOFF])
  expect(JSON.stringify(out)).toContain('Handoff from Codex (veyr) is in the chat bar')
  const none = await $.command.run({ command: 'shadow', args: 'handoff cursor', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 160 } })
  expect(JSON.stringify(none)).toContain('no recent session matching')
})

test('the summary model also reports the decisions made, sent with the summary', async ($, on) => {
  const calls = fakes(on, {
    sessions: [CODEX],
    draft: 'SUMMARY: Codex added model costs to the admin page. Tests pass.\nDECISIONS:\n- Show costs per model, not per provider\n- Keep the dropdown',
  })
  await claudeReplies($)
  const digest = calls.fetches.find(f => f.url.endsWith(`/sessions/${CODEX.id}/digest`))
  expect(digest?.body.text).toBe('Codex added model costs to the admin page. Tests pass.')
  expect(digest?.body.decisions).toEqual(['Show costs per model, not per provider', 'Keep the dropdown'])
})

test('a quiet session gets a model-written summary, once', async ($, on) => {
  const calls = fakes(on, { sessions: [CODEX], draft: 'Codex added model costs to the admin page; tests pass. The dropdown is still open.' })
  await claudeReplies($)
  const digest = calls.fetches.find(f => f.url.endsWith(`/sessions/${CODEX.id}/digest`))
  expect(digest?.body).toMatchObject({ by: 'haiku' })
  expect(digest?.body.text).toContain('model costs')
  const summaryPrompt = calls.drafts.find(d => d.system?.includes('summarise one coding-agent session'))
  expect(summaryPrompt?.prompt).toContain('Files changed: D:\\veyr\\chat.html')
})

test('a question naming an agent brings that session\'s full timeline', async ($, on) => {
  const calls = fakes(on, { sessions: [ANTIGRAVITY, { ...CODEX, needs_digest: false }] })
  await claudeReplies($)
  const ui = await sidebar($, 'terminal')
  await ui.input({ key: 'ask', text: 'what did codex do in veyr today?' })
  expect(calls.forks[0]).toContain('Detail for the sessions this question is about')
  expect(calls.forks[0]).toContain('files changed: D:\\veyr\\chat.html (2x)')
  expect(calls.forks[0]).toContain('commands: pytest')
  expect(calls.fetches.some(f => f.url.endsWith(`/sessions/${CODEX.id}`))).toBe(true)
  expect(calls.fetches.some(f => f.url.endsWith(`/sessions/${ANTIGRAVITY.id}`))).toBe(false)
  await ui.unmount()
})

test('AGENTS says when an agent\'s sessions cannot be read; CORE MEMORY shows the size', async ($, on) => {
  fakes(on, { sessions: [ANTIGRAVITY], problems: { codex: 'permission denied' } })
  await claudeReplies($)
  await $.prompt.submit({ text: 'keep going with it', wait: false, origin: { kind: 'composer' } })
  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /can't read codex: permission denied/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /3\.0 MB/ })).toBeDefined()
  await ui.unmount()
})

test('side questions know what the other agents are doing', async ($, on) => {
  const calls = fakes(on, { sessions: [{ ...CODEX, needs_digest: false }] })
  await claudeReplies($)
  const ui = await sidebar($, 'terminal')
  await ui.input({ key: 'ask', text: 'what is codex up to?' })
  expect(calls.forks[0]).toContain("What Shadow has seen the user's other coding agents doing")
  expect(calls.forks[0]).toContain('Codex in veyr (idle')
  await ui.unmount()
})

test('subagent turns and interrupted turns get no ghost', async ($, on) => {
  const calls = fakes(on)
  await claudeReplies($, SITUATION, { agentId: 'agent-1' })
  await $.turn.complete({ answer: SITUATION, durationMs: 10, isAborted: true, reason: 'aborted', turnId: 't2' })
  expect(paths(calls)).not.toContain('/predict')
})

test("Claude Code's own next-prompt guess gives way to the ghost", async ($, on) => {
  const calls = fakes(on)
  await claudeReplies($)
  calls.suggested.length = 0
  await $.prompt.suggest({ text: 'commit the changes', origin: { kind: 'suggestion' } })
  expect(calls.suggested).toEqual([GHOST])
})

test('an unreadable daemon answer is not mistaken for the daemon being down', async ($, on) => {
  const calls = fakes(on, { badJson: true })
  await claudeReplies($)
  expect(calls.status.filter(s => s !== undefined)).toEqual([])
  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /offline/ })).toBeUndefined()
  await ui.unmount()
})

test('with the daemon down, the sidebar says so and prompts still go through', async ($, on) => {
  const calls = fakes(on, { isDown: true })
  await claudeReplies($)
  const ui = await sidebar($, 'terminal')
  expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /link:OFFLINE/ })).toBeDefined()
  expect(calls.status.at(-1)).toMatch(/shadow offline/)
  await ui.unmount()
  const r = await $.prompt.submit({ text: 'carry on', wait: false, origin: { kind: 'composer' } })
  expect(r).toMatchObject({ text: 'carry on' })
  expect(calls.suggested).toEqual([])
})
