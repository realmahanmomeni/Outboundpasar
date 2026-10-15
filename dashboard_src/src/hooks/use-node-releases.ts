interface NodeReleaseResult {
  latestVersion: string | null
  releaseUrl: string | null
  isLoading: boolean
  hasUpdate: (currentVersion: string | null) => boolean
}

/** GitHub node release checks disabled. */
export function useNodeReleases(): NodeReleaseResult {
  return {
    latestVersion: null,
    releaseUrl: null,
    isLoading: false,
    hasUpdate: () => false,
  }
}
