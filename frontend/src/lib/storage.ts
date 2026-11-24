/**
 * localStorage that never throws. Storage can be missing, blocked (private
 * windows, site-data settings) or full; the app must keep working without it.
 */

export function readJson(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch {
    return null;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Unavailable or over quota: state lasts for this page only.
  }
}
