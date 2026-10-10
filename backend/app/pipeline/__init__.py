"""Ingestion pipeline: Gmail -> auth check -> masking -> classify -> extract -> reconcile."""

from typing import Final

# Bump whenever parsing/classification logic changes; messages processed by an older
# version can then be found (messages.pipeline_version < PIPELINE_VERSION) and reprocessed.
PIPELINE_VERSION: Final = 1
