import { describe, it, expect, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { useFocusModality } from './useFocusModality'

function Probe() {
  useFocusModality()
  return <input aria-label="field" />
}

const modality = () => document.documentElement.dataset.modality

beforeEach(() => {
  delete document.documentElement.dataset.modality
})

/** A text field matches `:focus-visible` whenever it is focused, clicked or
 * tabbed — that is the spec, not a browser quirk. So the focus ring cannot
 * be gated on `:focus-visible` alone without ringing every input the
 * moment you click into it. It is gated on modality instead. */
describe('useFocusModality', () => {
  it('starts with no modality set, so rings show until proven otherwise', () => {
    render(<Probe />)

    // Defaulting to "pointer" would hide the ring from a keyboard user who
    // has not touched a mouse yet — the expensive direction to be wrong in.
    expect(modality()).toBeUndefined()
  })

  it('switches to pointer on a mouse press', () => {
    render(<Probe />)
    fireEvent.pointerDown(document.body)

    expect(modality()).toBe('pointer')
  })

  it('switches back to keyboard on Tab', () => {
    render(<Probe />)
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'Tab' })

    expect(modality()).toBe('keyboard')
  })

  it('does not treat typing into a clicked field as keyboard navigation', () => {
    render(<Probe />)
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'a' })

    // You clicked into the field and started typing. Ringing it now would
    // be the exact behaviour this hook exists to remove.
    expect(modality()).toBe('pointer')
  })

  it('counts arrow keys, which drive a select without Tab', () => {
    render(<Probe />)
    fireEvent.pointerDown(document.body)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })

    expect(modality()).toBe('keyboard')
  })
})
