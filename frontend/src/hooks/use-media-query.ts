import * as React from "react"

export function useMediaQuery(query: string) {
  const getMatches = React.useCallback(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
    [query],
  )
  const [matches, setMatches] = React.useState(getMatches)

  React.useEffect(() => {
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [query])

  return matches
}
