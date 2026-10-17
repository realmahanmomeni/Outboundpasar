import { describe, expect, it } from 'bun:test'

import {
  buildSubscriptionFormatUrl,
  countZipArchiveEntries,
  isDownloadSubscriptionFormat,
  isEmptyOutlineSubscriptionContent,
  isEmptyWireGuardSubscriptionArchive,
  resolveSubscriptionPanelBaseUrl,
  subscriptionDownloadExtension,
  subscriptionDownloadMimeType,
  type SubscriptionContentFormat,
} from './subscription-config'

describe('subscription format helpers', () => {
  it('classifies download vs copy formats by format id', () => {
    expect(isDownloadSubscriptionFormat('clash')).toBe(true)
    expect(isDownloadSubscriptionFormat('clash_meta')).toBe(true)
    expect(isDownloadSubscriptionFormat('sing_box')).toBe(true)
    expect(isDownloadSubscriptionFormat('wireguard')).toBe(true)
    expect(isDownloadSubscriptionFormat('links')).toBe(false)
    expect(isDownloadSubscriptionFormat('links_base64')).toBe(false)
    expect(isDownloadSubscriptionFormat('xray')).toBe(false)
  })

  it('maps download extensions and mime types', () => {
    expect(subscriptionDownloadExtension('wireguard')).toBe('zip')
    expect(subscriptionDownloadExtension('clash_meta')).toBe('yaml')
    expect(subscriptionDownloadExtension('sing_box')).toBe('json')
    expect(subscriptionDownloadMimeType('xray')).toContain('json')
  })

  it('preserves absolute subscription host for format URLs', () => {
    const base = resolveSubscriptionPanelBaseUrl('https://panel.example.test:2053/sub/token')
    expect(base).toBe('https://panel.example.test:2053/sub/token')
    expect(buildSubscriptionFormatUrl('https://panel.example.test:2053/sub/token', 'clash_meta')).toBe(
      'https://panel.example.test:2053/sub/token/clash_meta',
    )
  })

  it('uses dashboard origin for relative subscription paths', () => {
    const originalWindow = globalThis.window
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).window = { location: { origin: 'https://dash.example.test' } }
    try {
      expect(resolveSubscriptionPanelBaseUrl('/sub/token')).toBe('https://dash.example.test/sub/token')
    } finally {
      if (originalWindow === undefined) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        delete (globalThis as any).window
      } else {
        globalThis.window = originalWindow
      }
    }
  })

  const copyFormats: SubscriptionContentFormat[] = ['links', 'links_base64', 'xray', 'outline']
  copyFormats.forEach(format => {
    it(`keeps ${format} on copy path`, () => {
      expect(isDownloadSubscriptionFormat(format)).toBe(false)
    })
  })

  it('detects empty outline JSON object', () => {
    expect(isEmptyOutlineSubscriptionContent('{}')).toBe(true)
    expect(isEmptyOutlineSubscriptionContent('  {}  ')).toBe(true)
    expect(isEmptyOutlineSubscriptionContent('{"server":"x"}')).toBe(false)
    expect(isEmptyOutlineSubscriptionContent('[]')).toBe(false)
  })

  it('counts ZIP entries via end-of-central-directory record', () => {
    const emptyZip = new Uint8Array([
      0x50, 0x4b, 0x05, 0x06, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ])
    expect(countZipArchiveEntries(emptyZip)).toBe(0)
    expect(isEmptyWireGuardSubscriptionArchive(emptyZip)).toBe(true)
  })
})
