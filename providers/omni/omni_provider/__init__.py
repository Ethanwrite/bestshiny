from provider_sdk import NotConfiguredProvider


class OmniProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("omni")
