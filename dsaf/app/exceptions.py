"""Custom exception hierarchy for DSAF."""


class DSAFException(Exception):
    """Base exception for all DSAF errors."""


class SurveyMapNotFoundError(DSAFException):
    """Raised when a survey map file cannot be found."""


class PatternValidationError(DSAFException):
    """Raised when a pattern fails validation against its survey map."""


class BrowserContextError(DSAFException):
    """Raised when browser context creation or operation fails."""


class ProxyBlockedError(DSAFException):
    """Raised when the proxy (or direct connection) is blocked by the target server."""


class PageFingerprintMismatchError(DSAFException):
    """Raised when the current page fingerprint does not match the expected branch."""


class HoneypotDetectedError(DSAFException):
    """Raised when an attempt is made to fill a detected honeypot field."""


class SurveyCompletionError(DSAFException):
    """Raised when the survey completion flow encounters an unrecoverable error."""
