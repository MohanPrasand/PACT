import { adminToken } from "./adminToken";
import { ApiError } from "./errors";
import { http, type HttpOptions } from "./http";

/** Admin API client: sends the admin token, calls only `/admin/*`. A 401/403 clears the token. */
export async function adminApi<T>(path: string, opts: HttpOptions = {}): Promise<T> {
  if (!path.startsWith("/admin/")) throw new Error(`adminApi only calls /admin/*, got ${path}`);
  const token = adminToken.get();
  if (!token) throw new ApiError(401, "AUTH_REQUIRED", "Admin token required");
  try {
    return await http<T>(path, { ...opts, headers: { ...opts.headers, Authorization: `Bearer ${token}` } });
  } catch (e) {
    if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
      adminToken.clear();
      window.dispatchEvent(new Event("pact:admin-logout"));
    }
    throw e;
  }
}
