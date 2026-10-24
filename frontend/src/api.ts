import type {
  Case,
  CompanyCase,
  CompanyOverview,
  CompanySettings,
  CustomerCase,
  CustomerCaseSummary,
  CustomerOrder,
  QueueItem,
} from "./types";

function getApiBase(): string {
  const envBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (typeof window !== "undefined") {
    if (window.location.hostname === "127.0.0.1") {
      return (envBase ?? "http://localhost:8000").replace("localhost", "127.0.0.1");
    }
    if (window.location.hostname === "localhost") {
      return (envBase ?? "http://127.0.0.1:8000").replace("127.0.0.1", "localhost");
    }
  }
  return envBase ?? "http://localhost:8000";
}
const base = getApiBase();
export type User = { id: string; email: string; role: "customer" | "manager" };
type ApiUser = Omit<User, "role"> & { role: "customer" | "reviewer" };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? "The request could not be completed.");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const getCase = (id: string) => request<Case | CustomerCase>(`/cases/${id}`);
export const getCustomerOrders = () => request<CustomerOrder[]>("/customer/orders");
export const getCustomerCases = () => request<CustomerCaseSummary[]>("/customer/cases");
export const getQueue = () => request<QueueItem[]>("/reviews");
export const getCompanyOverview = () => request<CompanyOverview>("/company/overview");
export const getCompanyCases = (search = "", status = "") => {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (status) params.set("status", status);
  const query = params.toString();
  return request<CompanyCase[]>(`/company/cases${query ? `?${query}` : ""}`);
};
export const getCompanySettings = () => request<CompanySettings>("/company/settings");
export const updateCompanySettings = (threshold_paise: number) =>
  request<CompanySettings>("/company/settings", {
    method: "PUT",
    body: JSON.stringify({ threshold_paise }),
  });
export const getMe = async (): Promise<User> => {
  const user = await request<ApiUser>("/auth/me");
  return { ...user, role: user.role === "reviewer" ? "manager" : user.role };
};

export const login = (email: string, password: string): Promise<User> =>
  request<ApiUser>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  }).then((user) => ({
    ...user,
    role: user.role === "reviewer" ? "manager" : "customer",
  }));

export const logout = () => request<void>("/auth/logout", { method: "POST" });

export async function decide(caseId: string, decision: "approve" | "deny", note?: string) {
  return request<{ case_id: string; status: string; idempotent_replay: boolean }>(
    `/reviews/${caseId}/decision`,
    { method: "POST", body: JSON.stringify({ decision, note: note || null }) },
  );
}

export async function streamChat(
  message: string,
  caseId: string | null,
  onEvent: (type: string, data: Record<string, unknown>) => void,
) {
  const response = await fetch(`${base}/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, case_id: caseId }),
  });
  if (!response.ok || !response.body) throw new Error("The conversation could not be started.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const event of events) {
      const type = event.match(/^event: (.+)$/m)?.[1];
      const data = event.match(/^data: (.+)$/m)?.[1];
      if (type && data) onEvent(type, JSON.parse(data) as Record<string, unknown>);
    }
  }
}
