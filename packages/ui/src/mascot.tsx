export type MascotState = "waiting" | "thinking" | "working" | "done";

const ART: Record<MascotState, string> = {
  waiting: new URL("../../../design/mascot/heli-idle.svg", import.meta.url).href,
  thinking: new URL("../../../design/mascot/heli-thinking.svg", import.meta.url).href,
  working: new URL("../../../design/mascot/heli-working.svg", import.meta.url).href,
  done: new URL("../../../design/mascot/heli-done.svg", import.meta.url).href,
};

const LABEL: Record<MascotState, string> = {
  waiting: "Ожидает",
  thinking: "Думает",
  working: "Работает",
  done: "Готово",
};

export function Mascot({ state, size = 160 }: { state: MascotState; size?: number }) {
  return (
    <img
      className="mascot"
      src={ART[state]}
      width={size}
      height={size}
      alt=""
      data-mascot-state={state}
    />
  );
}

export function mascotLabel(state: MascotState): string {
  return LABEL[state];
}

export function avatarFor(theme: "light" | "dark"): string {
  const file = theme === "dark" ? "avatar-idle-dark.svg" : "avatar-idle.svg";
  return new URL(`../../../design/mascot/${file}`, import.meta.url).href;
}
