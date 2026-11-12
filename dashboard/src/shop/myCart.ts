/** The human's cart id, kept in this browser only. Falls back to a per-session id if storage is unavailable. */
const KEY = "merchantlab.myCartId";
let memo: string | null = null;

export function myCartId(): string {
  try {
    const v = window.localStorage.getItem(KEY);
    if (v) return v;
  } catch {
    /* storage blocked */
  }
  if (memo) return memo;
  const id = `my-${Math.random().toString(36).slice(2, 10)}`;
  memo = id;
  try {
    window.localStorage.setItem(KEY, id);
  } catch {
    /* storage blocked */
  }
  return id;
}

/** Route alias "my" resolves to the browser-local id. */
export const resolveCartId = (routeId: string) => (routeId === "my" ? myCartId() : routeId);
