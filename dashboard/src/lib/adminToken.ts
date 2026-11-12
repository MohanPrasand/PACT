// The admin token lives in sessionStorage and is only ever read by the admin API client.
// The storefront client (lib/shopApi.ts) does not import this module.
const KEY = "pact.admin.token";

export const adminToken = {
  get(): string | null {
    try {
      const t = sessionStorage.getItem(KEY);
      if (t === null) return null;
      // A stored value that is not plain visible ASCII can never be sent in a header: discard it.
      if (!/^[\x21-\x7E]+$/.test(t)) {
        sessionStorage.removeItem(KEY);
        return null;
      }
      return t;
    } catch {
      return null;
    }
  },
  set(token: string): void {
    try {
      sessionStorage.setItem(KEY, token);
    } catch {
      /* storage unavailable */
    }
  },
  clear(): void {
    try {
      sessionStorage.removeItem(KEY);
    } catch {
      /* storage unavailable */
    }
  },
};
