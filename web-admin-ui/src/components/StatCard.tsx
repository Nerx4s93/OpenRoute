export function StatCard({
  glyph,
  value,
  label,
}: {
  glyph: string;
  value: string;
  label: string;
}) {
  return (
    <div className="card">
      <div className="glyph" aria-hidden="true">{glyph}</div>
      <div className="num">{value}</div>
      <div className="lbl">{label}</div>
    </div>
  );
}
