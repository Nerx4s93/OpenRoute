import type { ReactNode } from "react";

export function Section({
  icon,
  title,
  note,
  children,
  id,
}: {
  icon: string;
  title: string;
  note?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className="section page-enter" aria-labelledby={id}>
      <div className="section-head">
        <div className="section-title">
          <span className="section-icon" aria-hidden="true">{icon}</span>
          <h2 id={id}>{title}</h2>
        </div>
        {note ? <span className="section-note">{note}</span> : null}
      </div>
      {children}
    </section>
  );
}
