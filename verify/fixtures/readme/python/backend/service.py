class Ledger:
    def __init__(self) -> None:
        self.entries: list[int] = []

    def record(self, amount: int) -> int:
        self.entries.append(amount)
        return sum(self.entries)


def process_payment(amount: int, ledger: Ledger) -> int:
    fee = amount // 10
    return ledger.record(amount - fee)


def unused_report_a(rows: list[int]) -> int:
    return sum(rows) + len(rows)


def unused_report_b(rows: list[int]) -> int:
    return max(rows, default=0) - min(rows, default=0)


def unused_report_c(rows: list[int]) -> float:
    return sum(rows) / max(1, len(rows))


def unused_report_d(rows: list[int]) -> str:
    return ",".join(str(row) for row in rows)


def unused_report_e(rows: list[int]) -> dict[str, int]:
    return {"count": len(rows), "total": sum(rows)}


def unused_report_f(rows: list[int]) -> int:
    return sum(row * row for row in rows)
