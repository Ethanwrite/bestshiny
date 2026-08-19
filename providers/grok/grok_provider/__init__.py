from provider_sdk import NotConfiguredProvider


class GrokProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("grok")
