import logging

# Keep expected failures out of the test output. assertLogs still sees them.
logging.getLogger("skyguard").addHandler(logging.NullHandler())
