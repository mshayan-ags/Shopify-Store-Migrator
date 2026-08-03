class MigrationError(RuntimeError):
    pass


class ConfigurationError(MigrationError):
    pass


class ShopifyAPIError(MigrationError):
    def __init__(self, message, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ValidationError(MigrationError):
    pass
