import { http, type HttpOptions } from "./http";

/** Storefront API client: public, no credentials, calls only `/merchant/*`. Never imports the admin token. */
export function shopApi<T>(path: string, opts: HttpOptions = {}): Promise<T> {
  if (!path.startsWith("/merchant/")) throw new Error(`shopApi only calls /merchant/*, got ${path}`);
  return http<T>(path, opts);
}
