import { useId, type ReactNode } from "react";

/**
 * Heli mascot, direction «Визор»: a helicopter cabin seen from the front, a dark
 * glass visor and glowing eyes behind it. Emotion lives in the eyes; the rotor
 * and the hover add tempo. Colors come from tokens.css (--heli-*), motion from
 * the .heli-* rules there, so the same markup works in both themes.
 */
export type MascotState = "waiting" | "thinking" | "working" | "done" | "error";

const LABEL: Record<MascotState, string> = {
  waiting: "Ожидает",
  thinking: "Думает",
  working: "Работает",
  done: "Готово",
  error: "Ошибка",
};

const VISOR = { x: 36, y: 46, width: 48, height: 24, rx: 12 };

function cross(cx: number, cy: number, r: number) {
  return `M${cx - r} ${cy - r} L${cx + r} ${cy + r} M${cx + r} ${cy - r} L${cx - r} ${cy + r}`;
}

function Eyes({ state, clip }: { state: MascotState; clip: string }): ReactNode {
  switch (state) {
    case "thinking":
      return (
        <g className="heli-look">
          <rect x="48" y="50" width="7" height="10" rx="3.5" />
          <rect x="65" y="49" width="7" height="12" rx="3.5" />
        </g>
      );
    case "working":
      return (
        <>
          <rect x="44" y="56" width="13" height="5" rx="2.5" />
          <rect x="63" y="56" width="13" height="5" rx="2.5" />
          <clipPath id={clip}>
            <rect {...VISOR} />
          </clipPath>
          <g clipPath={`url(#${clip})`}>
            <rect className="heli-scan" x="56" y="46" width="8" height="24" />
          </g>
        </>
      );
    case "done":
      return <path className="heli-line" d="M46 62 Q51 53 56 62 M64 62 Q69 53 74 62" strokeWidth="4.5" />;
    case "error":
      return <path className="heli-line heli-x" d={`${cross(51, 58, 4)} ${cross(69, 58, 4)}`} strokeWidth="3.6" />;
    default:
      return (
        <g className="heli-blink">
          <rect x="47" y="51" width="8" height="14" rx="4" />
          <rect x="65" y="51" width="8" height="14" rx="4" />
        </g>
      );
  }
}

export function Mascot({ state, size = 160 }: { state: MascotState; size?: number }) {
  const clip = `heli-visor-${useId().replace(/:/g, "")}`;
  return (
    <svg
      className={`mascot heli heli-${state}`}
      viewBox="0 0 120 120"
      width={size}
      height={size}
      data-mascot-state={state}
      aria-hidden="true"
    >
      <g className="heli-hover">
        <g className="heli-body">
          <g transform={state === "error" ? "rotate(-9 60 24)" : undefined}>
            <rect className="heli-rotor" x="18" y="21" width="84" height="4.5" rx="2.25" />
          </g>
          <rect className="heli-ink" x="57.5" y="23" width="5" height="13" rx="2" />
          <rect className="heli-ink" x="27" y="33" width="66" height="56" rx="28" />
          <rect className="heli-visor" {...VISOR} />
          <g className="heli-eyes">
            <Eyes state={state} clip={clip} />
          </g>
          <path className="heli-skid" d="M47 88 L44 99 M73 88 L76 99 M34 99 H86" />
        </g>
      </g>
    </svg>
  );
}

export function mascotLabel(state: MascotState): string {
  return LABEL[state];
}
