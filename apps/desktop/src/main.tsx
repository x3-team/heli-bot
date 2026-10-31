import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "@heli/ui";
import "@heli/ui/tokens.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("root missing");
}

createRoot(root).render(
  <StrictMode>
    <Shell />
  </StrictMode>,
);
