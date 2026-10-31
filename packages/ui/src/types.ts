export type MascotState =
  | "idle"
  | "thinking"
  | "working"
  | "needs-help"
  | "done"
  | "error"
  | "limit";

export type AppView =
  | "chat"
  | "usage"
  | "schedules"
  | "credentials"
  | "memory"
  | "settings";

export type Risk = "read" | "write" | "danger";

export type ConfirmDecision = "approved" | "edited" | "rejected";

export interface ChatSummary {
  id: string;
  title: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
}

export interface StepView {
  id: string;
  index: number;
  tool: string;
  summary: string;
  risk: Risk;
  status: string;
}

export interface ConfirmationView {
  actionId: string;
  risk: Risk;
  title: string;
  detail: string;
}

export interface UsageView {
  weekPercent: number;
  monthPercent: number;
  weekResetLabel: string;
  blocked: boolean;
}

export interface ScheduleView {
  id: string;
  label: string;
  enabled: boolean;
}

export interface CredentialView {
  id: string;
  site: string;
  username: string;
}

export interface MemoryView {
  id: string;
  text: string;
}

export interface SettingsView {
  consentPdn: boolean;
  consentTraining: boolean;
  runCommandEnabled: boolean;
}
