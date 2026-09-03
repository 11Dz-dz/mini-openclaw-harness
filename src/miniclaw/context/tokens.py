import math


class CharacterTokenEstimator:
    def __init__(self, chars_per_token: int = 4) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self.chars_per_token = chars_per_token

    def estimate(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token)
