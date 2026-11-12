import { adminApi } from "../lib/adminApi";
import type { AuditEvent, AuditVerify, Overview, Policy, RequestDetail, RequestView } from "./types";

const enc = encodeURIComponent;

export const api = {
  overview: () => adminApi<Overview>("/admin/overview"),
  list: (state?: string) => adminApi<{ authorizations: RequestView[] }>("/admin/authorizations", { query: { state, limit: 200 } }),
  detail: (id: string) => adminApi<RequestDetail>(`/admin/authorizations/${enc(id)}`),
  approve: (id: string, request_hash: string, note: string) =>
    adminApi<RequestView>(`/admin/authorizations/${enc(id)}/approve`, { method: "POST", body: { request_hash, note } }),
  deny: (id: string, reason: string) => adminApi<RequestView>(`/admin/authorizations/${enc(id)}/deny`, { method: "POST", body: { reason } }),
  revoke: (id: string) => adminApi<RequestView>(`/admin/authorizations/${enc(id)}/revoke`, { method: "POST" }),
  cancel: (id: string) => adminApi<RequestView>(`/admin/authorizations/${enc(id)}/cancel`, { method: "POST" }),
  reconcile: (id: string) => adminApi<RequestView>(`/admin/authorizations/${enc(id)}/reconcile`, { method: "POST" }),
  policies: () => adminApi<{ policies: Policy[] }>("/admin/policies"),
  updatePolicy: (id: string, rules: Record<string, unknown>, expected_version: number) =>
    adminApi<{ id: string; scope: string; rules: Record<string, unknown>; version: number }>(`/admin/policies/${enc(id)}`, {
      method: "PUT",
      body: { rules, expected_version },
    }),
  audit: (request_id?: string, event_type?: string) =>
    adminApi<{ events: AuditEvent[] }>("/admin/audit", { query: { request_id, event_type, limit: 500 } }),
  verify: () => adminApi<AuditVerify>("/admin/audit/verify"),
};
