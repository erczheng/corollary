import { useEffect, useRef, useState } from 'react'
import { Chip } from './Chip'
import { ChevronDownIcon } from './icons'
import { useUIStore } from '../lib/store'
import { CHAT_SUGGESTIONS, type StrategyProposal } from '../lib/mockData'
import { formatDateTimeET, formatTimeET } from '../lib/format'

/** The Research chat (PRD.md §8.5).
 *
 * **Labelled as a scripted shell, on screen, permanently.** The LLM layer is
 * Phase 4 and this is Phase 1 mock data, so replies come from a keyword table
 * in `research.ts`. Every other fixture in the app is self-evidently a fixture
 * — a table of invented numbers reads as invented — but a fluent sentence
 * reads as a considered answer, which makes an unlabelled scripted chat the
 * one place here that could actually mislead someone. The chip says so and the
 * fallback reply says so.
 *
 * Opens empty on purpose, with suggested prompts. That makes the designed
 * empty state the default view and the populated transcript one click away, so
 * both are reachable without contriving a sequence — and the suggestions make
 * the keyword routing discoverable instead of a guessing game.
 */
export function ResearchChat() {
  const chat = useUIStore((s) => s.chat)
  const send = useUIStore((s) => s.sendChatMessage)
  const [draft, setDraft] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  // Follows the conversation down as it grows. Only on a new message, so it
  // does not fight you scrolling back through the transcript.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [chat.length])

  function submit(text: string) {
    send(text)
    setDraft('')
  }

  return (
    <section
      aria-labelledby="chat-heading"
      className="flex flex-col rounded-lg border border-outline-warm bg-surface-container-lowest"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
        <h2 id="chat-heading" className="text-title-lg text-on-surface">
          Chat
        </h2>
        <div className="flex items-center gap-2">
          <ChatHistoryMenu />
          {/* `caution`, not `error`: a shell standing in for a model is a stage
              of the build, not a fault. */}
          <Chip
            variant="caution"
            title="Replies come from a fixed table, not a language model. The live model arrives with the LLM layer in Phase 4."
          >
            Scripted — Phase 1
          </Chip>
        </div>
      </div>

      <div
        role="log"
        aria-label="Conversation"
        aria-live="polite"
        className="min-h-0 max-h-[28rem] flex-1 overflow-y-auto px-4 py-4"
      >
        {chat.length === 0 ? (
          <div>
            <p className="max-w-prose text-body-md text-on-surface-variant">
              Ask about this account and its data. Replies are scripted for now — the model itself
              arrives in Phase 4, and anything it cannot answer it will say so rather than guess.
            </p>
            <p className="mt-4 text-caption uppercase tracking-wide text-on-surface-variant">
              Try
            </p>
            <div className="mt-2 flex flex-col items-start gap-2">
              {CHAT_SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => submit(suggestion)}
                  className="rounded-full border border-outline px-3 py-1 text-left text-caption text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-low"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ul className="space-y-4">
            {chat.map((message) => (
              <li key={message.id} className={message.role === 'user' ? 'text-right' : ''}>
                <div
                  className={`inline-block max-w-[42ch] rounded-xl px-3 py-2 text-left ${
                    message.role === 'user'
                      ? 'bg-primary-container text-on-primary-container'
                      : 'bg-surface-container-low text-on-surface'
                  }`}
                >
                  <p className="text-body-md">{message.text}</p>
                  {message.proposal ? <ProposalCard proposal={message.proposal} /> : null}
                </div>
                <p className="mt-1 text-caption text-on-surface-variant">
                  {message.role === 'user' ? 'You' : 'Corollary'} · {formatTimeET(message.at)}
                </p>
              </li>
            ))}
          </ul>
        )}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (draft.trim() !== '') submit(draft)
        }}
        className="flex items-center gap-2 border-t border-outline-warm px-4 py-3"
      >
        <label htmlFor="chat-input" className="sr-only">
          Message
        </label>
        <input
          id="chat-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Ask about a strategy, a candidate, or your risk config…"
          className="min-w-0 flex-1 rounded border border-outline bg-surface px-3 py-2 text-body-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
        />
        <button
          type="submit"
          disabled={draft.trim() === ''}
          className="rounded bg-primary px-4 py-2 text-label-md text-on-primary disabled:pointer-events-none disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  )
}


/** Past conversations, anchored to the chat header.
 *
 * A menu rather than a sidebar: the chat is one column of a three-column page
 * and a permanent list would cost more width than a feature you reach for
 * between conversations is worth.
 *
 * **New chat is the only way a transcript is filed**, and it is disabled while
 * the transcript is empty — there is nothing to file, and you are already in a
 * new chat. Opening an archived conversation files the current one on the way
 * out, so switching never silently discards what is on screen.
 *
 * The list is a record, not a preference: nothing here edits or deletes a
 * conversation. Phase 4 replaces what generates the replies and none of this
 * changes shape.
 */
function ChatHistoryMenu() {
  const chat = useUIStore((s) => s.chat)
  const history = useUIStore((s) => s.chatHistory)
  const newChat = useUIStore((s) => s.newChat)
  const openChat = useUIStore((s) => s.openChat)

  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Same dismissal contract as the notification bell: Escape and a click
  // outside, both torn down when the menu closes.
  useEffect(() => {
    if (!open) return

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    function onPointerDown(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }

    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('mousedown', onPointerDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  const empty = chat.length === 0

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={`Chat history — ${history.length} saved`}
        className="flex items-center gap-1.5 rounded border border-outline px-2.5 py-1 text-label-md text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-low"
      >
        History
        <span className="text-on-surface-variant">{history.length}</span>
        <ChevronDownIcon className="h-3 w-3 text-on-surface-variant" />
      </button>

      {open ? (
        <div
          role="menu"
          aria-label="Chat history"
          className="absolute right-0 z-20 mt-2 w-80 rounded-lg border border-outline-warm bg-surface-container-lowest"
        >
          <div className="flex items-center justify-between border-b border-outline-warm px-3 py-2">
            <span className="text-caption uppercase tracking-wide text-on-surface-variant">
              Conversations
            </span>
            <button
              type="button"
              disabled={empty}
              onClick={() => {
                newChat()
                setOpen(false)
              }}
              title={
                empty
                  ? 'This conversation is empty — nothing to save'
                  : 'Save this conversation and start a new one'
              }
              className="rounded border border-outline px-2 py-1 text-caption text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50"
            >
              New chat
            </button>
          </div>

          {history.length === 0 ? (
            // Says what would be here and how it gets here, rather than
            // leaving a blank box that could equally mean "broken".
            <p className="px-3 py-4 text-caption text-on-surface-variant">
              No saved conversations yet. Starting a new chat files the one on screen here.
            </p>
          ) : (
            <ul className="max-h-72 overflow-y-auto py-1">
              {history.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      openChat(c.id)
                      setOpen(false)
                    }}
                    className="flex w-full flex-col gap-0.5 px-3 py-2 text-left transition-colors duration-base ease-standard hover:bg-surface-container-low"
                  >
                    <span className="truncate text-body-md text-on-surface">{c.title}</span>
                    <span className="text-caption text-on-surface-variant">
                      {formatDateTimeET(c.at)} · {c.messages.length} messages
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}

/** A strategy the assistant offered.
 *
 * Accepting lands a **draft** and says so on the button. §5.3's promotion gate
 * is what moves a strategy past draft, and the LLM proposing one earns no
 * exemption — origin never buys one anywhere else in the system either (§6.2).
 *
 * Whether it has already been accepted is derived from the strategy list
 * rather than tracked separately: a second copy of "was this added" is a second
 * thing that can disagree with the list you can see. */
function ProposalCard({ proposal }: { proposal: StrategyProposal }) {
  const strategies = useUIStore((s) => s.strategies)
  const accept = useUIStore((s) => s.acceptProposal)
  const added = strategies.some((s) => s.name === proposal.name)

  return (
    <div className="mt-3 rounded border border-outline-warm bg-surface p-3">
      <p className="text-data-md text-on-surface">{proposal.name}</p>
      <p className="mt-1 text-caption text-on-surface-variant">{proposal.summary}</p>
      <ul className="mt-2 space-y-1">
        {proposal.rules.map((rule) => (
          <li key={rule} className="text-caption text-on-surface-variant">
            · {rule}
          </li>
        ))}
      </ul>
      {/* No YAML body: the schema and the indicator whitelist are Phase 4, and
          printing rules a validator has never checked would invite trusting
          them. These are stated as intent, which is all a proposal honestly is
          before there is a document to validate. */}
      <p className="mt-2 text-caption text-on-surface-variant">
        Rules are stated as intent. The declarative document and its whitelist validation land with
        the strategy runtime.
      </p>
      {added ? (
        <p className="mt-3 text-caption text-on-surface-variant">
          Added as a draft. Promote it from the strategy list once it has a backtest.
        </p>
      ) : (
        <button
          type="button"
          onClick={() => accept(proposal)}
          className="mt-3 rounded border border-primary px-3 py-1 text-label-md text-primary transition-colors duration-base ease-standard hover:bg-primary-container hover:text-on-primary-container"
        >
          Add as draft
        </button>
      )}
    </div>
  )
}
