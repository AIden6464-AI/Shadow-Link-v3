import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const SITUATION = "I'll set up a Docker container for the dev environment."
const GHOST = "no, don't use docker. use the venv"
const ALT = 'keep it short, bullet points'

type Calls = {
  fetches: { url: string; body: any }[]
  suggested: string[]
  filled: string[]
  forks: string[]
  status: (string | undefined)[]
}

/** What Claude Code itself draws above the prompt when Shadow passes. */
const ENGINE_BAND = 'claude-code-default-band'

/** A fake Shadow Link daemon and model beneath the plugin. */
function fakes(on: On, opts: { isDown?: boolean } = {}): Calls {
  const calls: Calls = { fetches: [], suggested: [], filled: [], forks: [], status: [] }
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text key="engine">{ENGINE_BAND}</Text>
  })
  on('ui.status', ($, e) => {
    calls.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => ({ value: undefined }))
  on('http.fetch', ($, e) => {
    if (opts.isDown) return { deny: 'connection refused' }
    const body = e.init?.body ? JSON.parse(e.init.body) : null
    calls.fetches.push({ url: e.url, body })
    const path = new URL(e.url).pathname
    const reply =
      path === '/predict'
        ? {
            ghost: { id: 1, text: GHOST, bucket: 'rejection', confidence: 0.68 },
            alternates: [
              { id: 5, text: ALT, bucket: 'request', confidence: 0.3 },
              { id: 9, text: 'noise', bucket: 'request', confidence: 0.02 },
            ],
          }
        : path === '/aca/event'
          ? { context: [{ title: 'you rejected Docker for dev', type: 'reaction', content: 'use the venv', why: { seed_reason: 'lexical match 0.55' } }] }
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

function band($: Engine, surface: 'terminal' | 'desktop', isWorking = false) {
  return $.ui.mount({
    plugin: 'shadow-link',
    surface,
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking, maxRows: 12, bodyColumns: 80, scroll: { offset: 0, bodyRows: 12 }, view: {} },
  })
}

const paths = (c: Calls) => c.fetches.map(f => new URL(f.url).pathname)

for (const surface of ['terminal', 'desktop'] as const) {
  test(`${surface}: a Claude turn floats the ghost above the chat bar and in the prompt box`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    expect(paths(calls)).toContain('/predict')
    expect(calls.suggested).toEqual([GHOST])

    const ui = await band($, surface)
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /68% · rejection · 1\/2/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: low-confidence guesses are not shown`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    expect(await ui.find({ type: 'Text', text: 'noise' })).toBeUndefined()
    await ui.press({ key: 'next' })
    expect(await ui.find({ type: 'Text', text: ALT })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: use puts the ghost in the chat bar; sending it teaches Shadow`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    await ui.press({ key: 'use' })
    expect(calls.filled).toEqual([GHOST])

    await $.prompt.submit({ text: GHOST, wait: false, origin: { kind: 'composer' } })
    const ingest = calls.fetches.find(f => f.url.endsWith('/ingest'))
    expect(ingest?.body).toMatchObject({ question: SITUATION, response: GHOST })
    const up = calls.fetches.find(f => f.url.endsWith('/feedback'))
    expect(up?.body).toMatchObject({ id: 1, vote: 'up' })
    // The turn is answered: the band clears.
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    await ui.unmount()
  })

  test(`${surface}: wrong down-votes the ghost and shows the next guess`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    await ui.press({ key: 'wrong' })
    const down = calls.fetches.find(f => f.url.endsWith('/feedback'))
    expect(down?.body).toMatchObject({ id: 1, vote: 'down' })
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: ALT })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: why shows what the situation brought back`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    await ui.press({ key: 'why' })
    expect(await ui.find({ type: 'Text', text: /you rejected Docker for dev/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: a side question is answered over the session without touching it`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    await ui.input({ key: 'ask', text: 'did I ever decide on postgres?' })
    expect(calls.forks.length).toBe(1)
    expect(calls.forks[0]).toContain('did I ever decide on postgres?')
    expect(calls.forks[0]).toContain('use the venv') // recalled from Shadow Link's memory
    expect(await ui.find({ type: 'Text', text: /docker compose for the database only/ })).toBeDefined()
    await ui.press({ key: 'send-answer' })
    expect(calls.filled).toEqual(['Yes: docker compose for the database only.'])
    await ui.unmount()
  })

  test(`${surface}: /shadow <question> answers in the band, not in the conversation`, async ($, on) => {
    const calls = fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    const out = await $.command.run({ command: 'shadow', args: 'what did I decide about docker?', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(calls.forks.length).toBe(1)
    expect(JSON.stringify(out)).not.toContain('docker compose for the database only')
    expect(await ui.find({ type: 'Text', text: /docker compose for the database only/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: @shadow in the chat bar asks Shadow and never reaches Claude`, async ($, on) => {
    const calls = fakes(on)
    const clock = mock.clock(on)
    await claudeReplies($)
    const ui = await band($, surface)
    const r = await $.prompt.submit({ text: '@shadow what did I decide about docker?', wait: false, origin: { kind: 'composer' } })
    expect(r).toHaveProperty('drop')
    await clock.advance(1)
    expect(calls.forks.length).toBe(1)
    expect(calls.forks[0]).toContain('what did I decide about docker?')
    expect(calls.fetches.some(f => f.url.endsWith('/ingest'))).toBe(false)
    expect(calls.filled).toContain('') // the chat bar is cleared
    expect(await ui.find({ type: 'Text', text: /docker compose for the database only/ })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: stays out of the way while Claude is working`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await band($, surface, true)
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
    await ui.unmount()
  })

  test(`${surface}: hide removes the band until /shadow`, async ($, on) => {
    fakes(on)
    await claudeReplies($)
    const ui = await band($, surface)
    await ui.press({ key: 'hide' })
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: ENGINE_BAND })).toBeDefined()
    await $.command.run({ command: 'shadow', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(await ui.find({ type: 'Text', text: GHOST })).toBeDefined()
    await ui.unmount()
  })
}

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

test('with the daemon down, nothing floats and prompts still go through', async ($, on) => {
  const calls = fakes(on, { isDown: true })
  await claudeReplies($)
  const ui = await band($, 'terminal')
  expect(await ui.find({ type: 'Text', text: GHOST })).toBeUndefined()
  expect(calls.status.at(-1)).toMatch(/shadow offline/)
  await ui.unmount()
  const r = await $.prompt.submit({ text: 'carry on', wait: false, origin: { kind: 'composer' } })
  expect(r).toMatchObject({ text: 'carry on' })
  expect(calls.suggested).toEqual([])
})
