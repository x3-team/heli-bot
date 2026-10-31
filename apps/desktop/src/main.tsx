import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { Shell, checkForAppUpdates } from "@heli/ui";
import "@heli/ui/tokens.css";

function Boot() {
  useEffect(() => {
    void checkForAppUpdates();
  }, []);
  return <Shell />;
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("root missing");
}

createRoot(root).render(
  <StrictMode>
    <Boot />
  </StrictMode>,
);
