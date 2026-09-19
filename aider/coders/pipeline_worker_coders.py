from .editor_editblock_coder import EditorEditBlockCoder
from .editor_whole_coder import EditorWholeFileCoder


class PipelineWorkerWholeCoder(EditorWholeFileCoder):
    """Whole-file worker for pipeline mode.

    The orchestrator decides which single file a task may touch, so the
    worker must not pull more files into its own context.
    """

    edit_format = "pipeline-worker-whole"

    def check_for_file_mentions(self, content):
        return None


class PipelineWorkerDiffCoder(EditorEditBlockCoder):
    """Search/replace worker for pipeline mode, used for large files."""

    edit_format = "pipeline-worker-diff"

    def check_for_file_mentions(self, content):
        return None
