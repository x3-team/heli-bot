/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_HELI_API_BASE?: string;
  readonly DEV: boolean;
  readonly PROD: boolean;
  readonly MODE: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
