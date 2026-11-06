export class Ledger {
  private entries: number[] = [];

  record(amount: number): number {
    this.entries.push(amount);
    return this.entries.reduce((total, value) => total + value, 0);
  }
}

export function processPayment(amount: number, ledger: Ledger): number {
  const fee = Math.floor(amount / 10);
  return ledger.record(amount - fee);
}

export function unusedReportA(rows: number[]): number {
  return rows.reduce((total, value) => total + value, 0) + rows.length;
}

export function unusedReportB(rows: number[]): number {
  return Math.max(...rows) - Math.min(...rows);
}

export function unusedReportC(rows: number[]): number {
  return rows.reduce((total, value) => total + value, 0) / Math.max(1, rows.length);
}

export function unusedReportD(rows: number[]): string {
  return rows.map((row) => String(row)).join(",");
}

export function unusedReportE(rows: number[]): Record<string, number> {
  return { count: rows.length, total: rows.reduce((total, value) => total + value, 0) };
}

export function unusedReportF(rows: number[]): number {
  return rows.reduce((total, value) => total + value * value, 0);
}
