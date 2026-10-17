interface Release {
  version: string
  url: string
  isPrerelease?: boolean
}

interface XrayReleaseResult {
  latestVersion: string | null
  releaseUrl: string | null
  versions: Release[]
  isLoading: boolean
  hasUpdate: (currentVersion: string | null) => boolean
}

/** GitHub Xray release checks disabled. */
export function useXrayReleases(): XrayReleaseResult {
  return {
    latestVersion: null,
    releaseUrl: null,
    versions: [],
    isLoading: false,
    hasUpdate: () => false,
  }
}
