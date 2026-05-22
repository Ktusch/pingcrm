"""Named constants by domain.

Per the project's large-codebase conventions, provider names and error
identifiers live here as enums rather than as bare string literals scattered
across the codebase — so they are greppable, typo-proof, and refactorable.
"""

from app.constants.error_ids import AppError, ErrorId
from app.constants.providers import Provider

__all__ = ["AppError", "ErrorId", "Provider"]
