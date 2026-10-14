interface VersionCheckResult {
  hasUpdate: boolean
  latestVersion: string | null
  currentVersion: string | null
  releaseUrl: string | null
  isLoading: boolean
}

interface UseVersionCheckOptions {
  enabled?: boolean
}

/** GitHub release checks disabled; version badge uses local app version only. */
export function useVersionCheck(currentVersion: string | null, _options: UseVersionCheckOptions = {}): VersionCheckResult {
  const cleanCurrentVersion = currentVersion?.replace(/^v/, '') || null

  return {
    hasUpdate: false,
    latestVersion: null,
    currentVersion: cleanCurrentVersion,
    releaseUrl: null,
    isLoading: false,
  }
}
