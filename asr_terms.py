# -*- coding: utf-8 -*-
"""ASR 确定性后处理：只在固定词典命中时替换，避免误改正文。

三类规则：
- greek：中文读法的希腊字母 -> 符号（西塔->θ、阿尔法->α ...）
- abbr ：被逐字母拆开的英文缩写 -> 合并（M C U->MCU、I I C->I2C ...）
- units：中文单位名 -> 常用符号（毫安->mA、千欧->kΩ ...）

用法：
    from asr_terms import apply
    clean = apply(raw_text, terms=meta_terms)      # 只替换词典白名单命中的片段
    clean = apply(raw_text, enabled=False)         # 完全关闭

规则集合可在调用时按类别开关，也可通过 extra_rules 追加自定义词条。
"""
import re

# 中文读法 -> 符号。仅收录歧义极低的常见读法。
GREEK = {
    "阿尔法": "α", "阿法": "α",
    "贝塔": "β",
    "伽马": "γ",
    "德尔塔": "δ", "代尔塔": "δ",
    "伊普西龙": "ε", "艾普西龙": "ε", "艾普西隆": "ε", "厄普西隆": "ε",
    "泽塔": "ζ",
    "西塔": "θ", "西他": "θ",
    "约塔": "ι",
    "卡帕": "κ",
    "兰姆达": "λ", "拉姆达": "λ", "兰布达": "λ",
    "克西": "ξ",
    "奥米克戎": "ο", "奥密克戎": "ο",
    "西格玛": "σ",
    "法伊": "φ",
    "普西": "ψ", "普赛": "ψ",
    "欧米伽": "ω", "欧米茄": "ω", "奥米伽": "ω",
}

# 英文拼读形式 -> 符号。新接线后模型偶尔直接输出 theta/alpha 等英文词。
# 仅收录长度 >=4 的词，降低误伤；区分大小写（AlphaGo 不会被改成 αGo）。
LATIN_GREEK = {
    "theta": "θ", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "lambda": "λ", "sigma": "σ", "omega": "ω", "omicron": "ο",
}

# 不做替换的常见专名，避免把 AlphaGo / Bertelsmann 之类改成符号。
GREEK_PROTECT = ("阿尔法狗", "阿法狗", "贝塔斯曼")

# 被逐字母拆开的英文缩写 -> 合并。按“规范形”匹配，长词优先。
SPLIT_ABBR = {
    "MEMS": r"M\s*E\s*M\s*S",
    "MCU": r"M\s*C\s*U",
    "MPU": r"M\s*P\s*U",
    "ADC": r"A\s*D\s*C",
    "DAC": r"D\s*A\s*C",
    "I2C": r"I\s*(?:I\s*|2\s*)C",
    "SPI": r"S\s*P\s*I",
    "UART": r"U\s*A\s*R\s*T",
    "GPIO": r"G\s*P\s*I\s*O",
    "PWM": r"P\s*W\s*M",
    "USB": r"U\s*S\s*B",
    "CPU": r"C\s*P\s*U",
    "LED": r"L\s*E\s*D",
    "RAM": r"R\s*A\s*M",
    "ROM": r"R\s*O\s*M",
    "ISR": r"I\s*S\s*R",
    "SMD": r"S\s*M\s*D",
    "TE": r"T\s*E",
    "RF": r"R\s*F",
    "PCB": r"P\s*C\s*B",
}

# 中文单位名 -> 符号。仅收录无歧义的单位。
UNITS = {
    "欧姆": "Ω", "千欧": "kΩ", "兆欧": "MΩ",
    "毫安": "mA", "微安": "μA",
    "毫伏": "mV", "微伏": "μV",
    "微法": "μF", "纳法": "nF", "皮法": "pF",
    "兆赫": "MHz", "千赫": "kHz", "赫兹": "Hz",
    "毫秒": "ms", "微秒": "μs", "纳秒": "ns",
    "毫瓦": "mW", "瓦特": "W",
    "伏特": "V", "安培": "A", "摄氏度": "°C",
}


def _norm_term(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).upper()


def _term_set(terms) -> set:
    return {_norm_term(t) for t in terms} if terms else set()


def apply(text: str, terms=None, enabled: bool = True,
          use_greek: bool = True, use_abbr: bool = True, use_units: bool = True,
          extra_rules: dict = None) -> str:
    """对一段转写文本做确定性替换，返回替换后的文本。

    terms: 课程术语表；提供时，缩写规则只替换“规范形在术语表里”的词，
           避免把正文里的普通字母组合误合并。为 None 时启用全部缩写规则。
    """
    if not enabled or not text:
        return text
    out = text

    if use_greek:
        # 先遮蔽保护词，替换后再还原，避免把“阿尔法狗”改成“α狗”
        masks = {}
        for i, p in enumerate(GREEK_PROTECT):
            if p in out:
                token = f"\x00PROTECT{i}\x00"
                masks[token] = p
                out = out.replace(p, token)
        for src, dst in GREEK.items():
            out = out.replace(src, dst)
        for src, dst in LATIN_GREEK.items():
            out = re.sub(r"(?<![A-Za-z])" + src + r"(?![A-Za-z])", dst, out)
        for token, p in masks.items():
            out = out.replace(token, p)

    if use_abbr:
        # terms=None 表示不做白名单限制; terms=[] 表示白名单为空, 不替换缩写
        gated = terms is not None
        allowed = _term_set(terms)
        for canon, pat in SPLIT_ABBR.items():
            if gated and canon.upper() not in allowed:
                continue
            # 前后必须是字母边界，避免命中更长的英文单词
            rx = re.compile(r"(?<![A-Za-z])" + pat + r"(?![A-Za-z])")
            out = rx.sub(canon, out)

    if use_units:
        for src, dst in UNITS.items():
            out = out.replace(src, dst)

    if extra_rules:
        for src, dst in extra_rules.items():
            out = out.replace(src, dst)
    return out


def stats(text: str, terms=None) -> dict:
    """返回每类规则在 text 上的命中次数，供评测使用。"""
    base = text
    n_greek = len([1 for _ in re.finditer(
        "|".join(map(re.escape, GREEK.keys())), base)])
    n_abbr = 0
    gated = terms is not None
    allowed = _term_set(terms)
    for canon, pat in SPLIT_ABBR.items():
        if gated and canon.upper() not in allowed:
            continue
        n_abbr += len(re.findall(r"(?<![A-Za-z])" + pat + r"(?![A-Za-z])", base))
    n_units = sum(base.count(k) for k in UNITS)
    return {"greek": n_greek, "abbr": n_abbr, "units": n_units}
