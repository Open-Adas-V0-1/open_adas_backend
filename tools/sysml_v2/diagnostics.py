from dataclasses import dataclass

_SEVERITY = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}


@dataclass
class Diagnostic:
    message: str
    line: int  # 1-based
    column: int  # 1-based
    severity: str  # "Error" | "Warning" | "Info" | "Hint"

    @classmethod
    def from_lsp(cls, raw: dict) -> "Diagnostic":
        start = raw["range"]["start"]
        return cls(
            message=raw.get("message", ""),
            line=start["line"] + 1,
            column=start["character"] + 1,
            severity=_SEVERITY.get(raw.get("severity", 1), str(raw.get("severity"))),
        )

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
        }
