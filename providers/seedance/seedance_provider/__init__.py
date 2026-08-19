from provider_sdk import NotConfiguredProvider


class SeedanceProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("seedance")
