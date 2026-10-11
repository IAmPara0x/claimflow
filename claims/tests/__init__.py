import logging

# Keep test output readable; the claims logger is chatty at INFO.
logging.getLogger("claims").setLevel(logging.WARNING)
