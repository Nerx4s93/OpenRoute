export function StatCard({
  glyph,
  value,
  label,
  title,
  hint,
}: {
  glyph: string;
  value: string;
  label: string;
  title?: string;
  hint?: string;
}) {
  return (
    <div className="card" title={title}>
      <div className="glyph" aria-hidden="true">{glyph}</div>
      <div className="num">{value}</div>
      <div className="lbl">{label}</div>
      {hint ? <div className="card-hint">{hint}</div> : null}
    </div>
  );
}
