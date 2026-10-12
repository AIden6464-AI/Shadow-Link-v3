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

export type ShadowView = 'ghost' | 'why' | 'answer'

declare module 'claude-code' {
  interface PluginState {
    'shadow-link': {
      ghost: ShadowGhost | null
      situation: string
      view: ShadowView
      why: ShadowWhy[] | null
      answer: ShadowAnswer | null
      isOnline: boolean
      isHidden: boolean
    }
  }
}
