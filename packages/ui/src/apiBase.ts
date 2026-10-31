/** Production API origin (no trailing slash). Empty in dev → Vite proxies `/v1`. */
export function resolveApiBase(override?: string): string {
  if (override !== undefined && override.trim()) {
    return override.trim().replace(/\/$/, "");
  }
  const baked = import.meta.env.VITE_HELI_API_BASE;
  if (typeof baked === "string" && baked.trim()) {
    return baked.trim().replace(/\/$/, "");
  }
  if (import.meta.env.DEV) {
    return "";
  }
  return "";
}
