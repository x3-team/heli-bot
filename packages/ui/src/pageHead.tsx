/** Page title in the cabinet style: serif heading and a short explanation. */
export function PageHead({ title, lede, aside }: { title: string; lede?: string; aside?: string }) {
  return (
    <header className="page-head">
      {aside ? <span className="page-aside">{aside}</span> : null}
      <h1>{title}</h1>
      {lede ? <p className="muted">{lede}</p> : null}
    </header>
  );
}
