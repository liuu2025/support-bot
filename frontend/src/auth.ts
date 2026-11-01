/**
 * Where the widget gets its identity.
 *
 * In production the host site (which already knows who is logged in) issues a
 * short-lived signed JWT and hands it to the widget, either:
 *   - by defining `window.SUPPORT_BOT_TOKEN` before the bundle loads, or
 *   - via `postMessage({ type: "support-bot-token", token })` from a parent
 *     window whose origin is listed in VITE_TRUSTED_PARENT_ORIGINS.
 * The backend verifies the token; the chat never asks the user who they are.
 *
 * In local dev (AUTH_MODE=dev) the UI can mint tokens via /api/dev/token.
 */

import { apiUrl } from "./config";

export type Role = "customer" | "admin";

declare global {
  interface Window {
    SUPPORT_BOT_TOKEN?: string;
    SUPPORT_BOT_ADMIN_TOKEN?: string;
  }
}

const KEYS: Record<Role, string> = {
  customer: "support-bot.customerToken",
  admin: "support-bot.adminToken",
};

const listeners = new Set<(role: Role) => void>();

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function getToken(role: Role): string | null {
  const injected = role === "customer" ? window.SUPPORT_BOT_TOKEN : window.SUPPORT_BOT_ADMIN_TOKEN;
  return injected || read(KEYS[role]);
}

export function setToken(role: Role, token: string | null): void {
  try {
    if (token) sessionStorage.setItem(KEYS[role], token);
    else sessionStorage.removeItem(KEYS[role]);
  } catch {
    /* storage unavailable (private mode) — keep going without persistence */
  }
  if (role === "customer") window.SUPPORT_BOT_TOKEN = token ?? undefined;
  else window.SUPPORT_BOT_ADMIN_TOKEN = token ?? undefined;
  listeners.forEach((fn) => fn(role));
}

export function onTokenChange(fn: (role: Role) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const trustedOrigins = (import.meta.env.VITE_TRUSTED_PARENT_ORIGINS ?? "")
  .split(",")
  .map((o: string) => o.trim())
  .filter(Boolean);

window.addEventListener("message", (e: MessageEvent) => {
  if (!trustedOrigins.includes(e.origin)) return;
  const data = e.data as { type?: string; token?: string; role?: Role };
  if (data?.type === "support-bot-token" && typeof data.token === "string") {
    setToken(data.role === "admin" ? "admin" : "customer", data.token);
  }
});

/** Dev only: mint a token from the backend's /api/dev/token endpoint. */
export async function devLogin(role: Role, customerId?: string): Promise<void> {
  const res = await fetch(apiUrl("/api/dev/token"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role === "admin" ? { admin: true } : { customer_id: customerId }),
  });
  if (!res.ok) throw new Error(`dev login failed: ${res.status}`);
  setToken(role, (await res.json()).token);
}

export async function devCustomers(): Promise<{ id: string; name: string; email: string }[] | null> {
  try {
    const res = await fetch(apiUrl("/api/dev/customers"));
    return res.ok ? res.json() : null;
  } catch {
    return null;
  }
}
