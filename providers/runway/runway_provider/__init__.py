from provider_sdk import NotConfiguredProvider


class RunwayProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("runway")


__all__ = ["RunwayProvider"]
