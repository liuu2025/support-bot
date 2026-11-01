/**
 * Build-time configuration (Vite env vars).
 *
 * VITE_API_BASE_URL — backend origin when the UI is served from a different
 *   host than the API. Empty = same origin (nginx routes /api to the backend).
 */
const env = import.meta.env;

export const API_BASE = (env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export const apiUrl = (path: string) => `${API_BASE}${path}`;
