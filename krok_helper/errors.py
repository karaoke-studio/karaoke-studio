class ProcessingError(RuntimeError):
    """Raised when the ffmpeg pipeline cannot continue."""


class MissingMediaToolError(ProcessingError):
    """An FFmpeg component could not be resolved."""

    def __init__(self, tool: str) -> None:
        self.tool = tool.lower().removesuffix(".exe")
        super().__init__(
            f"找不到 {tool}。请前往「全局设置 → 工具 → FFmpeg」安装或配置 FFmpeg 工具包。"
        )


class ExportCancelled(ProcessingError):
    """Raised when the user stops an export in progress."""
