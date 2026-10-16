export type ShadowAlt = { id: number | null; text: string; confidence: number; bucket: string }

export type ShadowGhost = {
  /** What Claude said that this ghost answers. */
  situation: string
  /** The ranked guesses; `index` is the one on show. */
  options: ShadowAlt[]
  index: number
}

export type ShadowWhy = { title: string; type: string; reason: string }

export type ShadowAnswer = { question: string; text: string; isPending: boolean }

export type ShadowStats = { corpus: number | null; learned: number }

/** One coding-agent session Shadow is tracking (Claude Code, Codex, Antigravity). */
export type ShadowAgent = {
  id: string
  label: string
  projectName: string
  state: 'active' | 'idle' | 'ended'
  doing: string
  goal: string
  openThread: string
  last: number
  digest: string
  needsDigest: boolean
}

export type ShadowThread = { session: string; agent: string; projectName: string; text: string }

declare module 'claude-code' {
  interface PluginState {
    'shadow-link': {
      ghost: ShadowGhost | null
      situation: string
      /** Why the ghost on show was guessed; null while not asked (or loading, see isWhyOpen). */
      why: ShadowWhy[] | null
      isWhyOpen: boolean
      answer: ShadowAnswer | null
      isOnline: boolean
      isWaiting: boolean
      stats: ShadowStats
      /** The person's latest messages this session, oldest first: their voice right now. */
      recent: string[]
      /** True while a model drafts the ghost for the turn that just ended. */
      isDrafting: boolean
      /** Other agents' recent sessions, newest first. */
      agents: ShadowAgent[]
      /** Questions agents left unanswered in this project. */
      threads: ShadowThread[]
      /** The agent row expanded in the sidebar, if any. */
      openAgent: string | null
    }
  }
}
