"""Stable import facade for the Operator-managed Hermes skill tools.

Implementations are split by responsibility across the content, file, shared
validation, and Hermes manager modules. Mutations retain the existing policy
gates, dry-run defaults, path checks, and audit behavior.
"""

from operator_skill_content import (
    hermes_skill_create,
    hermes_skill_diff,
    hermes_skill_edit,
    hermes_skill_patch,
)
from operator_skill_files import (
    hermes_skill_copy,
    hermes_skill_delete,
    hermes_skill_sync_to_default,
    hermes_skill_write_file,
)

__all__ = [
    "hermes_skill_copy",
    "hermes_skill_create",
    "hermes_skill_delete",
    "hermes_skill_diff",
    "hermes_skill_edit",
    "hermes_skill_patch",
    "hermes_skill_sync_to_default",
    "hermes_skill_write_file",
]
