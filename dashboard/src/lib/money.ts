/** Display-only formatter: integer minor units (cents) -> "$79.00". No arithmetic on money anywhere else. */
export function formatMoney(minor: number, currency = "USD"): string {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(minor / 100);
  } catch {
    return `${(minor / 100).toFixed(2)} ${currency}`;
  }
}
