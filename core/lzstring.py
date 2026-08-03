"""LZString — the compression RPG Maker MV wraps its saves in.

An MV save file is ``LZString.compressToBase64(JSON.stringify(save))``, so
reading one means implementing the same algorithm the engine's JavaScript
uses. This is a direct port of the reference implementation (pieroxy's
lz-string, MIT), kept deliberately close to it: the two must agree
bit-for-bit or the game will not load what we write back.

Only the base64 variant is here, because that is the only one MV uses.

**Which base64 variant matters.** The library has been written two ways. The
older one compresses into sixteen-bit characters and then base64-encodes
those bytes; the newer one packs six bits straight into the base64 alphabet.
Both carry the SAME bit stream, so their output agrees character for
character — until the very end, where they pad differently.

That tail is not cosmetic. A game holding the older reader can return an
empty string from a save written the newer way, which is a save it will not
load. Every RPG Maker MV game checked ships the older one, and the older
form is read correctly by the newer reader as well, so that is the form
written here: the one both understand.
"""
import base64

_KEY_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_B64_INDEX = {c: i for i, c in enumerate(_KEY_B64)}
# The width the older form packs into before it base64-encodes the result.
_WIDE = 16


def compress_to_base64(uncompressed: str) -> str:
    if uncompressed is None:
        return ""
    # Sixteen bits per character, then those characters as big-endian bytes,
    # then ordinary base64 — which is what the engine's own library does.
    packed = _compress(uncompressed, _WIDE, chr)
    raw = b"".join(ord(c).to_bytes(2, "big") for c in packed)
    return base64.b64encode(raw).decode("ascii")


def decompress_from_base64(compressed: str) -> "str | None":
    if compressed is None:
        return ""
    if compressed == "":
        return None
    return _decompress(len(compressed), 32,
                       lambda index: _B64_INDEX.get(compressed[index], 0))


def _compress(uncompressed: str, bits_per_char: int, get_char) -> str:
    if uncompressed is None:
        return ""

    context_dictionary = {}
    context_dictionary_to_create = {}
    context_c = ""
    context_wc = ""
    context_w = ""
    context_enlarge_in = 2      # how many new entries before the width grows
    context_dict_size = 3
    context_num_bits = 2
    context_data = []
    context_data_val = 0
    context_data_position = 0

    for ii in range(len(uncompressed)):
        context_c = uncompressed[ii]
        if context_c not in context_dictionary:
            context_dictionary[context_c] = context_dict_size
            context_dict_size += 1
            context_dictionary_to_create[context_c] = True

        context_wc = context_w + context_c
        if context_wc in context_dictionary:
            context_w = context_wc
            continue

        if context_w in context_dictionary_to_create:
            if ord(context_w[0]) < 256:
                for _ in range(context_num_bits):
                    context_data_val = context_data_val << 1
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                value = ord(context_w[0])
                for _ in range(8):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            else:
                value = 1
                for _ in range(context_num_bits):
                    context_data_val = (context_data_val << 1) | value
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = 0
                value = ord(context_w[0])
                for _ in range(16):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            context_enlarge_in -= 1
            if context_enlarge_in == 0:
                context_enlarge_in = 2 ** context_num_bits
                context_num_bits += 1
            del context_dictionary_to_create[context_w]
        else:
            value = context_dictionary[context_w]
            for _ in range(context_num_bits):
                context_data_val = (context_data_val << 1) | (value & 1)
                if context_data_position == bits_per_char - 1:
                    context_data_position = 0
                    context_data.append(get_char(context_data_val))
                    context_data_val = 0
                else:
                    context_data_position += 1
                value = value >> 1

        context_enlarge_in -= 1
        if context_enlarge_in == 0:
            context_enlarge_in = 2 ** context_num_bits
            context_num_bits += 1

        context_dictionary[context_wc] = context_dict_size
        context_dict_size += 1
        context_w = context_c

    # Flush whatever is still in the buffer.
    if context_w != "":
        if context_w in context_dictionary_to_create:
            if ord(context_w[0]) < 256:
                for _ in range(context_num_bits):
                    context_data_val = context_data_val << 1
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                value = ord(context_w[0])
                for _ in range(8):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            else:
                value = 1
                for _ in range(context_num_bits):
                    context_data_val = (context_data_val << 1) | value
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = 0
                value = ord(context_w[0])
                for _ in range(16):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bits_per_char - 1:
                        context_data_position = 0
                        context_data.append(get_char(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            context_enlarge_in -= 1
            if context_enlarge_in == 0:
                context_enlarge_in = 2 ** context_num_bits
                context_num_bits += 1
            del context_dictionary_to_create[context_w]
        else:
            value = context_dictionary[context_w]
            for _ in range(context_num_bits):
                context_data_val = (context_data_val << 1) | (value & 1)
                if context_data_position == bits_per_char - 1:
                    context_data_position = 0
                    context_data.append(get_char(context_data_val))
                    context_data_val = 0
                else:
                    context_data_position += 1
                value = value >> 1

        context_enlarge_in -= 1
        if context_enlarge_in == 0:
            context_enlarge_in = 2 ** context_num_bits
            context_num_bits += 1

    # Mark the end of the stream.
    value = 2
    for _ in range(context_num_bits):
        context_data_val = (context_data_val << 1) | (value & 1)
        if context_data_position == bits_per_char - 1:
            context_data_position = 0
            context_data.append(get_char(context_data_val))
            context_data_val = 0
        else:
            context_data_position += 1
        value = value >> 1

    while True:
        context_data_val = context_data_val << 1
        if context_data_position == bits_per_char - 1:
            context_data.append(get_char(context_data_val))
            break
        context_data_position += 1

    return "".join(context_data)


def _decompress(length: int, reset_value: int, get_next_value) -> "str | None":
    dictionary = {i: i for i in range(3)}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    entry = ""
    result = []

    data_val = get_next_value(0)
    data_position = reset_value
    data_index = 1

    bits, maxpower, power = 0, 2 ** 2, 1
    while power != maxpower:
        resb = data_val & data_position
        data_position >>= 1
        if data_position == 0:
            data_position = reset_value
            data_val = get_next_value(data_index)
            data_index += 1
        bits |= (1 if resb > 0 else 0) * power
        power <<= 1

    next_ = bits
    if next_ == 0:
        bits, maxpower, power = 0, 2 ** 8, 1
        while power != maxpower:
            resb = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = reset_value
                data_val = get_next_value(data_index)
                data_index += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        c = chr(bits)
    elif next_ == 1:
        bits, maxpower, power = 0, 2 ** 16, 1
        while power != maxpower:
            resb = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = reset_value
                data_val = get_next_value(data_index)
                data_index += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        c = chr(bits)
    elif next_ == 2:
        return ""
    else:
        return ""

    dictionary[3] = c
    w = c
    result.append(c)

    while True:
        if data_index > length:
            return ""

        bits, maxpower, power = 0, 2 ** num_bits, 1
        while power != maxpower:
            resb = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = reset_value
                data_val = get_next_value(data_index)
                data_index += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1

        c = bits
        if c == 0:
            bits, maxpower, power = 0, 2 ** 8, 1
            while power != maxpower:
                resb = data_val & data_position
                data_position >>= 1
                if data_position == 0:
                    data_position = reset_value
                    data_val = get_next_value(data_index)
                    data_index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            dictionary[dict_size] = chr(bits)
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 1:
            bits, maxpower, power = 0, 2 ** 16, 1
            while power != maxpower:
                resb = data_val & data_position
                data_position >>= 1
                if data_position == 0:
                    data_position = reset_value
                    data_val = get_next_value(data_index)
                    data_index += 1
                bits |= (1 if resb > 0 else 0) * power
                power <<= 1
            dictionary[dict_size] = chr(bits)
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1

        if c in dictionary:
            entry = dictionary[c]
        elif c == dict_size:
            entry = w + w[0]
        else:
            return None

        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1
        w = entry

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1
