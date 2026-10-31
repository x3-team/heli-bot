import { useEffect, useRef } from "react";
import lottie from "lottie-web";

export type MascotState = "waiting" | "thinking" | "working" | "done";

const ART: Record<Exclude<MascotState, "working">, string> = {
  waiting: new URL("../../../design/mascot/heli-idle.svg", import.meta.url).href,
  thinking: new URL("../../../design/mascot/heli-thinking.svg", import.meta.url).href,
  done: new URL("../../../design/mascot/heli-done.svg", import.meta.url).href,
};

const WORKING_LOTTIE = new URL("../../../design/mascot/heli-working.lottie.json", import.meta.url).href;
const WORKING_SVG = new URL("../../../design/mascot/heli-working.svg", import.meta.url).href;

const LABEL: Record<MascotState, string> = {
  waiting: "Ожидает",
  thinking: "Думает",
  working: "Работает",
  done: "Готово",
};

function WorkingMascot({ size }: { size: number }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node) return;
    const anim = lottie.loadAnimation({
      container: node,
      renderer: "svg",
      loop: true,
      autoplay: true,
      path: WORKING_LOTTIE,
    });
    return () => anim.destroy();
  }, []);

  return (
    <div
      ref={host}
      className="mascot mascot-working"
      style={{ width: size, height: size }}
      data-mascot-state="working"
      aria-hidden="true"
    />
  );
}

export function Mascot({ state, size = 160 }: { state: MascotState; size?: number }) {
  if (state === "working") {
    return <WorkingMascot size={size} />;
  }

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

/** Animated working mascot via SMIL when Lottie is unavailable (e.g. tests). */
export function MascotWorkingSvg({ size = 160 }: { size?: number }) {
  return (
    <object
      className="mascot mascot-working"
      type="image/svg+xml"
      data={WORKING_SVG}
      width={size}
      height={size}
      aria-hidden="true"
      data-mascot-state="working"
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
