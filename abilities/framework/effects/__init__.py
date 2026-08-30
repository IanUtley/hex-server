"""Effect implementations grouped by the operation they perform.

The BOM dispatcher remains the compatibility entry point for now.  New code
should import an operation from this package rather than from a trigger
module, and leaf registration will migrate here incrementally.
"""

