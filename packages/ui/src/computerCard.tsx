import { useEffect, useState } from "react";

import { describe, markerPosition, type ConfirmRequest } from "./computer";
import { computerConfirm, type ConfirmQueue } from "./computerDesktop";

/** Card in the chat: "Heli хочет нажать «Провести»" with the screen and a marker. */
export function ComputerConfirmCard({ queue = computerConfirm }: { queue?: ConfirmQueue }) {
  const [request, setRequest] = useState<ConfirmRequest | null>(queue.pending());
  useEffect(() => queue.subscribe(setRequest), [queue]);
  if (!request) return null;
  const { action, risk, shot } = request;
  const marker = markerPosition(action);
  const danger = risk === "danger";
  return (
    <article className={`card computer-confirm${danger ? " danger" : ""}`} aria-live="assertive">
      <p className="muted">
        Шаг {request.step} · {danger ? "необратимое действие" : "действие на компьютере"}
      </p>
      <h2>Heli хочет {describe(action)}</h2>
      {request.thought ? <p className="muted">{request.thought}</p> : null}
      <div className="computer-shot">
        <img src={`data:image/png;base64,${shot.pngBase64}`} alt="Снимок экрана перед действием" />
        {marker ? <span className="computer-marker" style={marker} aria-hidden="true" /> : null}
      </div>
      <div className="row">
        <button type="button" data-emphasis={danger ? "danger" : "primary"} onClick={() => queue.answer("approve")}>
          Разрешить
        </button>
        {danger ? null : (
          <button type="button" onClick={() => queue.answer("all")}>
            Разрешать до конца задачи
          </button>
        )}
        <button type="button" onClick={() => queue.answer("reject")}>
          Отклонить
        </button>
      </div>
    </article>
  );
}
