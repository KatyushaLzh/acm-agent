"""Conservative, provider-neutral negotiation of rejected output budgets.

Provider messages are untrusted text. Only a named output parameter and an
explicit upper-bound constraint can lower a budget; context-window errors do
not describe a reusable output limit. No raw response text is retained.
"""

from __future__ import annotations

import re

from .provider import ProviderError


_PARAMETER = re.compile(r"(?<![a-z0-9_])max_(?:completion_|output_)?tokens(?![a-z0-9_])", re.I)
_EXCLUDED = re.compile(
    r"context(?:[_ -](?:window|length|limit))?|context_length_exceeded|"
    r"input[_ -]tokens|prompt[_ -]tokens|total[_ -]tokens|"
    r"unsupported|not supported|unrecognized|unknown parameter|"
    r"unexpected (?:keyword|parameter)|not (?:a )?(?:valid|allowed) parameter|"
    r"上下文|不支持|未知参数",
    re.I,
)
# Bounded integer lexemes prevent decimals, exponents and enormous integers
# from being partially accepted. Thousands separators are allowed.
_NUMBER = r"(?<![a-z0-9_.+-])([0-9]{1,12}(?:,[0-9]{3})*)(?![a-z0-9_]|\.[0-9]|,[0-9])"
_RANGE_NUMBER = r"(?<![\w.+-])([0-9]{1,12})(?![\w.])"
_BOUNDS = (
    re.compile(r"[\[(]\s*" + _RANGE_NUMBER + r"\s*[,，]\s*" + _RANGE_NUMBER + r"\s*[\])]", re.I),
    re.compile(r"\bbetween\s+" + _NUMBER + r"\s+and\s+" + _NUMBER, re.I),
    re.compile(
        r"(?:\bat most\s*|\bless than or equal to\s*|<=\s*|≤\s*|"
        r"\b(?:maximum|max allowed|upper limit|upper bound)(?:\s+(?:value|is|of|allowed|tokens|output|completion|supported|length|number|limit|:)){0,5}\s*[:=]?\s*|"
        r"不能超过\s*|不得超过\s*|最大(?:值|输出)?(?:为|是)?\s*[:：]?\s*)" + _NUMBER,
        re.I,
    ),
)
_TOO_LARGE = re.compile(
    r"too (?:large|high|big)|exceeds? (?:the )?(?:maximum|max|limit|output)|"
    r"greater than (?:the )?(?:maximum|max|limit)|above (?:the )?(?:maximum|max|limit)|"
    r"超出.{0,12}(?:上限|最大)|超过.{0,12}(?:上限|最大)|过大",
    re.I,
)


def next_output_token_limit(error: ProviderError, requested: int) -> int | None:
    """Suggest a strictly smaller budget for an explicit output-limit failure.

    A numeric limit supplied by the provider wins. A numberless "too large"
    error allows a bounded backoff down to 256 tokens. The caller must verify
    the new budget with an actual request before treating it as supported.
    """
    if (
        error.code != "invalid_request"
        or error.status not in (None, 400, 422)
        or isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested <= 1
    ):
        return None
    message = str(error)
    # Do not parse truncated text: a later qualification could change meaning.
    if len(message) > 8192 or _EXCLUDED.search(message):
        return None
    limits: list[int] = []
    numeric_constraint = False
    too_large = False
    for parameter in _PARAMETER.finditer(message):
        # Keep the bound attached to its parameter, not unrelated long prose.
        clause = message[parameter.end():parameter.end() + 384]
        too_large = too_large or bool(_TOO_LARGE.search(clause))
        for pattern in _BOUNDS:
            for match in pattern.finditer(clause):
                numeric_constraint = True
                values = [int(value.replace(",", "")) for value in match.groups()]
                upper = values[-1]
                if len(values) == 2 and values[0] > upper:
                    continue
                if 0 < upper < requested:
                    limits.append(upper)
    if limits:
        return min(limits)
    if numeric_constraint:
        return None
    if too_large and requested > 256:
        return max(256, requested // 2)
    return None
