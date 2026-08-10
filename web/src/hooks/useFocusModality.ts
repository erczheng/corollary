import { useEffect } from 'react'

/** Records how the user is currently driving the interface, as
 * `data-modality` on <html>, so the focus ring can be shown to keyboard
 * users and hidden from mouse users.
 *
 * `:focus-visible` alone does not achieve this. Per spec a **text field
 * always matches `:focus-visible` when focused**, including when you
 * clicked it — which is why every input and select in the order ticket
 * drew a ring on click while buttons did not. The modality gate is the
 * only way to separate the two cases.
 *
 * Defaults to keyboard: until the first pointer event we show rings. A
 * wrong guess in that direction is a visible ring nobody needed; the other
 * direction is an unnavigable interface. */
export function useFocusModality() {
  useEffect(() => {
    const root = document.documentElement

    const usePointer = () => {
      root.dataset.modality = 'pointer'
    }

    const useKeyboard = (e: KeyboardEvent) => {
      // Only navigation keys count. Typing into a field you clicked into
      // shouldn't suddenly ring it.
      if (e.key === 'Tab' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        root.dataset.modality = 'keyboard'
      }
    }

    window.addEventListener('pointerdown', usePointer, true)
    window.addEventListener('keydown', useKeyboard, true)
    return () => {
      window.removeEventListener('pointerdown', usePointer, true)
      window.removeEventListener('keydown', useKeyboard, true)
    }
  }, [])
}
