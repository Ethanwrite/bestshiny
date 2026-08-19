from provider_sdk import NotConfiguredProvider


class VeoOfficialProvider(NotConfiguredProvider):
    def __init__(self):
        super().__init__("veo_official")
