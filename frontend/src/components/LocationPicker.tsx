import { useEffect, useRef, useState } from 'react'
import { searchLocations } from '../api'
import type { LocationSuggestion } from '../types'

/** Location search with deterministic offline-gazetteer suggestions.
 *
 * The user picks a suggestion (resolving to real coordinates + a real IANA
 * timezone) or types "lat, lon". The resolved place is shown clearly; no
 * GeoJSON, AOI internals, cache keys, or FortyGuard internals are exposed.
 * Fully keyboard accessible (combobox/listbox, arrows, Enter, Escape). */
export default function LocationPicker({
  picked,
  onPick,
  onTextChange,
  id = 'hp-location',
}: {
  picked: LocationSuggestion | null
  onPick: (s: LocationSuggestion) => void
  onTextChange: (text: string) => void
  id?: string
}) {
  const [text, setText] = useState(picked?.display_name ?? '')
  const [suggestions, setSuggestions] = useState<LocationSuggestion[]>([])
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const [searching, setSearching] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const listId = `${id}-listbox`

  // Keep the field in sync when a suggestion is picked programmatically.
  useEffect(() => {
    if (picked) setText(picked.display_name)
  }, [picked])

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  function handleChange(value: string) {
    setText(value)
    onTextChange(value)
    setActiveIdx(-1)
    if (timer.current) clearTimeout(timer.current)
    if (!value.trim()) {
      setSuggestions([])
      setOpen(false)
      setSearching(false)
      return
    }
    setSearching(true)
    timer.current = setTimeout(() => {
      searchLocations(value)
        .then((s) => {
          setSuggestions(s)
          setOpen(true)
        })
        .finally(() => setSearching(false))
    }, 220)
  }

  function choose(s: LocationSuggestion) {
    setText(s.display_name)
    setOpen(false)
    setSuggestions([])
    setActiveIdx(-1)
    onPick(s)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || suggestions.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx((i) => (i + 1) % suggestions.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx((i) => (i - 1 + suggestions.length) % suggestions.length)
    } else if (e.key === 'Enter' && activeIdx >= 0) {
      e.preventDefault()
      choose(suggestions[activeIdx])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  const showEmpty = open && !searching && text.trim() && suggestions.length === 0

  return (
    <div className="location-wrap">
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeIdx >= 0 ? `${listId}-opt-${activeIdx}` : undefined}
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        onKeyDown={onKeyDown}
        onFocus={() => suggestions.length > 0 && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder="Search for a park, city, or place…"
        autoComplete="off"
      />
      {open && suggestions.length > 0 && (
        <ul className="suggestions" role="listbox" id={listId}>
          {suggestions.map((s, i) => {
            const parts = s.display_name.split(',')
            const name = parts[0]
            const region = parts.slice(1).join(',').trim()
            return (
              <li key={`${s.display_name}-${i}`} role="presentation">
                <button
                  type="button"
                  role="option"
                  id={`${listId}-opt-${i}`}
                  aria-selected={i === activeIdx}
                  className={i === activeIdx ? 'suggestion active' : 'suggestion'}
                  onMouseDown={(e) => {
                    e.preventDefault()
                    choose(s)
                  }}
                  onMouseEnter={() => setActiveIdx(i)}
                >
                  <span className="suggestion-icon" aria-hidden="true">
                    📍
                  </span>
                  <span className="suggestion-text">
                    <span className="suggestion-name">{name}</span>
                    {region ? <span className="suggestion-region">{region}</span> : null}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {showEmpty && (
        <p className="hint hint-empty" role="status">
          No matching place in the offline gazetteer — try a city, a known park,
          or coordinates like 38.6126, -121.5188.
        </p>
      )}
      {picked && (
        <p className="picked-location" role="status">
          <span aria-hidden="true">✓ </span>
          {picked.display_name}
          <span className="picked-tz"> · {picked.timezone.replace('_', ' ')}</span>
        </p>
      )}
    </div>
  )
}
