from __future__ import annotations


class MissingModelsFetcherStatus:
    """Small utility node so managers can identify this extension as a node pack."""

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    FUNCTION = "status"
    CATEGORY = "utils"
    DESCRIPTION = "Reports that ComfyUI Missing Models Fetcher is installed."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def status(self):
        return ("ComfyUI Missing Models Fetcher is installed.",)
