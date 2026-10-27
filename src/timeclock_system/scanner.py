"""The fingerprint scanner (spec Â§6.2); the matching loop lands in a later task."""

# Standard library imports
from dataclasses import dataclass, field
from uuid import UUID  # noqa: TC003  # the dataclass annotation is evaluated at runtime


@dataclass(frozen=True)
class GalleryPrint:
  """One enrolled print as the matcher consumes it."""

  print_id: UUID
  employee_id: int
  template: bytes = field(repr=False)  # R15: keep the template out of tracebacks and alerts
