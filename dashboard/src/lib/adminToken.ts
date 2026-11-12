// The admin token lives in sessionStorage and is only ever read by the admin API client.
// The storefront client (lib/shopApi.ts) does not import this module.
const KEY = "pact.admin.token";

export const adminToken = {
  get(): string | null {
    try {
      return sessionStorage.getItem(KEY);
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
