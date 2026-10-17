"""Panel driver errors (aligned with Outbound Center adapter contracts)."""


class PanelAdapterError(Exception):
    """Raised when a panel adapter operation fails."""


class PanelOperationUnsupportedError(PanelAdapterError):
    """The panel driver does not implement the requested operation."""


class PanelAuthError(PanelAdapterError):
    pass


class PanelConnectionError(PanelAdapterError):
    pass


class PanelResponseError(PanelAdapterError):
    def __init__(self, message: str, *, raw: dict | None = None):
        self.message = message
        self.raw = raw
        super().__init__(message)


class ClientExistsError(PanelAdapterError):
    def __init__(self, message: str, *, username: str | None = None, existing_user: dict | None = None):
        self.username = username
        self.existing_user = existing_user
        super().__init__(message)


class ClientNotFoundError(PanelAdapterError):
    pass


class InsufficientPermissionsError(PanelAdapterError):
    def __init__(self, message: str, *, raw: dict | None = None):
        self.message = message
        self.raw = raw
        super().__init__(message)
