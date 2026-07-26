"""report_builder tool: ghep cac section thanh bao cao markdown va luu file."""

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)

REPORTS_DIR = "reports"


class ReportSection(BaseModel):
    heading: str = Field(description="Section heading")
    content: str = Field(description="Section body in markdown or plain text")


class ReportBuilderParams(BaseModel):
    title: str = Field(description="Report title, used as the H1 and to name the file")
    sections: list[ReportSection] = Field(
        default_factory=list, description="Ordered list of report sections"
    )


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "report"


@tool(
    name="report_builder",
    description=(
        "Assemble a markdown report from a title and a list of sections (heading + content), "
        "save it as a .md file under the local 'reports' directory and return both the "
        "markdown text and the file path. Use this as the final step when the user asks for "
        "a report, summary document or comparison write-up."
    ),
    params=ReportBuilderParams,
)
def report_builder(params: ReportBuilderParams, ctx: ToolContext) -> ToolResult:
    try:
        if not params.title.strip():
            return ToolResult(ok=False, error="'title' must not be empty")

        parts = [f"# {params.title.strip()}"]
        for section in params.sections:
            parts.append(f"## {section.heading.strip()}")
            parts.append(section.content.strip())
        markdown = "\n\n".join(parts) + "\n"

        reports_dir = Path(REPORTS_DIR)
        reports_dir.mkdir(parents=True, exist_ok=True)
        # Namespace theo user/task de hai bao cao cung tieu de khong ghi de nhau
        prefix = f"u{ctx.user_id}-t{ctx.task_id or 0}-"
        path = reports_dir / f"{prefix}{_slugify(params.title)}.md"
        path.write_text(markdown, encoding="utf-8")
        return ToolResult(ok=True, data={"markdown": markdown, "path": str(path)})
    except Exception as e:
        logger.exception("report_builder failed")
        return ToolResult(ok=False, error=f"report_builder failed: {e}")
