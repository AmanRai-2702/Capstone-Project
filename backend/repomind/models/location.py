from pydantic import BaseModel, Field, model_validator


class SourceLocation(BaseModel):
    """1-indexed, inclusive line range inside a file."""
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_order(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self

    def __str__(self) -> str:
        return f"{self.file_path}:{self.start_line}-{self.end_line}"