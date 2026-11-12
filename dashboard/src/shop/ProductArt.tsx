const PALETTE: Record<string, [string, string]> = {
  peripherals: ["#ccfbf1", "#0f766e"],
  displays: ["#dbeafe", "#1d4ed8"],
  audio: ["#fce7f3", "#be185d"],
  datasets: ["#fef3c7", "#b45309"],
};

/** Generated inline-SVG art per category; no external images. */
export function ProductArt({ category, className = "" }: { category: string; className?: string }) {
  const [bg, fg] = PALETTE[category] ?? ["#e2e8f0", "#475569"];
  let glyph;
  switch (category) {
    case "peripherals":
      glyph = (
        <g fill={fg}>
          <rect x="22" y="38" width="76" height="34" rx="5" fillOpacity=".15" stroke={fg} strokeWidth="3" />
          {[0, 1, 2, 3, 4, 5].map((i) => <rect key={i} x={29 + i * 11} y="45" width="7" height="7" rx="1.5" />)}
          {[0, 1, 2, 3, 4].map((i) => <rect key={i} x={34 + i * 11} y="56" width="7" height="7" rx="1.5" />)}
        </g>
      );
      break;
    case "displays":
      glyph = (
        <g fill="none" stroke={fg} strokeWidth="3" strokeLinecap="round">
          <rect x="22" y="28" width="76" height="46" rx="5" fill={fg} fillOpacity=".15" />
          <path d="M60 74v10M44 86h32" />
        </g>
      );
      break;
    case "audio":
      glyph = (
        <g fill="none" stroke={fg} strokeWidth="4" strokeLinecap="round">
          <path d="M30 62V54a30 30 0 0 1 60 0v8" />
          <rect x="24" y="58" width="12" height="22" rx="4" fill={fg} />
          <rect x="84" y="58" width="12" height="22" rx="4" fill={fg} />
        </g>
      );
      break;
    case "datasets":
      glyph = (
        <g fill={fg} fillOpacity=".18" stroke={fg} strokeWidth="3">
          <ellipse cx="60" cy="34" rx="28" ry="9" />
          <path d="M32 34v32c0 5 12.5 9 28 9s28-4 28-9V34" />
          <path d="M32 50c0 5 12.5 9 28 9s28-4 28-9" fill="none" />
        </g>
      );
      break;
    default:
      glyph = (
        <g fill={fg} fillOpacity=".18" stroke={fg} strokeWidth="3" strokeLinejoin="round">
          <path d="M60 26l30 14v32l-30 14-30-14V40z" />
          <path d="M30 40l30 14 30-14M60 54v32" fill="none" />
        </g>
      );
  }
  return (
    <svg viewBox="0 0 120 110" role="img" aria-label={`${category} product`} className={className} preserveAspectRatio="xMidYMid slice">
      <rect width="120" height="110" fill={bg} />
      {glyph}
    </svg>
  );
}
