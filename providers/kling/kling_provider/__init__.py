from provider_sdk import NotConfiguredProvider


class KlingProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("kling")


__all__ = ["KlingProvider"]
