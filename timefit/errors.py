"""ユーザーにそのまま見せられる日本語メッセージを持つ例外"""


class TimeFitError(Exception):
    """このアプリの例外の基底クラス。str(e) をそのまま画面に出してよい。"""


class EngineNotRunningError(TimeFitError):
    pass


class EngineError(TimeFitError):
    pass


class ApiKeyMissingError(TimeFitError):
    pass


class ApiKeyInvalidError(TimeFitError):
    pass


class LLMError(TimeFitError):
    pass


class InputFileError(TimeFitError):
    pass


class ConfigError(TimeFitError):
    pass
