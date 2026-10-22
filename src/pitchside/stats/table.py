"""A small result table: what every analysis returns, and what gets cached.

Compact by design: columns once, rows as lists, numbers already rounded, so a
rendered table costs few tokens when it reaches an AI client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Table:
    columns: list[str]
    rows: list[list]
    title: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"title": self.title, "columns": self.columns, "rows": self.rows, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> Table:
        return cls(columns=d["columns"], rows=d["rows"], title=d.get("title", ""), notes=d.get("notes", []))

    def records(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def render(self, max_rows: int | None = None) -> str:
        rows = self.rows[:max_rows] if max_rows else self.rows
        cells = [[_fmt(v) for v in r] for r in rows]
        widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(self.columns)]
        numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) for i in range(len(self.columns))]

        def line(vals):
            return "  ".join(v.rjust(w) if num else v.ljust(w) for v, w, num in zip(vals, widths, numeric)).rstrip()

        out = [self.title] if self.title else []
        out += [line(self.columns), line(["-" * w for w in widths])] + [line(r) for r in cells]
        if max_rows and len(self.rows) > max_rows:
            out.append(f"... {len(self.rows) - max_rows} more rows")
        out += [f"note: {n}" for n in self.notes]
        return "\n".join(out)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)
