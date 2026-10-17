/** Matches backend ``oc_<panel_id>_d_<destination_config_id>`` virtual host tags. */
export function isOcDestinationInboundTag(tag?: string | null): boolean {
  if (!tag || !tag.startsWith('oc_')) return false
  const parts = tag.split('_')
  return parts.length >= 4 && parts[0] === 'oc' && parts[2] === 'd' && /^\d+$/.test(parts[1])
}
