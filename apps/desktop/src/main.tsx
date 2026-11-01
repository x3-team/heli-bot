import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell, UpdateNotice } from "@heli/ui";
import "@heli/ui/tokens.css";

function Boot() {
  return (
    <>
      <Shell />
      <UpdateNotice />
    </>
  );
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
