BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def decimal_to_base62(number: int) -> str:
    if number < 0:
        raise ValueError("number must be non-negative")
    if number == 0:
        return "0"

    alphabet = BASE62_ALPHABET
    base = len(alphabet)
    encoded = []
    while number:
        number, remainder = divmod(number, base)
        encoded.append(alphabet[remainder])
    return "".join(reversed(encoded))


def base62_to_decimal(code: str) -> int:
    if not code:
        raise ValueError("code must be non-empty")

    base = len(BASE62_ALPHABET)
    number = 0
    for char in code:
        index = BASE62_ALPHABET.find(char)
        if index == -1:
            raise ValueError("code contains a character outside Base62")
        number = number * base + index
    return number
