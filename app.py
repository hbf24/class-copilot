# -*- coding: utf-8 -*-
"""Class Copilot - 课堂录音转写与讲课复现平台
上传PPT -> 一键录音 -> qwen3-asr转写 -> DeepSeek对照PPT整理笔记/思维导图
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import httpx
import asr_terms
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 目录约定: 项目根下并列两个文件夹 —— 「课程库」(数据) 与「软件本体」(程序)。
#   项目根/课程库/...            真实课程目录树
#   项目根/软件本体/app.py        程序源码
#   项目根/.courses .covers ...   技术数据(全部 . 前缀隐藏)
# BASE   = 项目根(数据根);  APPDIR = 软件本体目录(源码/静态资源/配置)
_cc_base = os.environ.get("CC_BASE")
if _cc_base:
    BASE = Path(_cc_base).resolve()
elif getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
# 源码放在「软件本体」子目录里时, 数据根取它的上一级
if not _cc_base and not getattr(sys, "frozen", False):
    _here = Path(__file__).resolve().parent
    if _here.name == "软件本体" or (_here.parent / "课程库").is_dir():
        BASE = _here.parent
APPDIR = Path(__file__).resolve().parent if not getattr(sys, "frozen", False) else Path(sys.executable).resolve().parent
if not (APPDIR / "static").is_dir():
    # 打包后静态资源随 exe 解包, 软件本体目录取 exe 附近那个
    for _cand in (APPDIR.parent / "软件本体", APPDIR.parent, APPDIR):
        if (_cand / "static").is_dir():
            APPDIR = _cand
            break
DATA = BASE
LIB = DATA / "课程库"          # 真实课程目录树: 课程/课堂/资料
STATIC = APPDIR / "static"
# 打包成单文件 exe 且被移出项目目录时, 静态资源从 PyInstaller 解包目录读取
if not STATIC.is_dir() and getattr(sys, "_MEIPASS", None):
    STATIC = Path(sys._MEIPASS) / "static"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def run_quiet(*args, **kwargs):
    """调用 ffmpeg/ffprobe/soffice 等外部命令时不弹出黑色控制台窗口。"""
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(*args, **kwargs)

DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_URL = "https://api.deepseek.com/chat/completions"
SEG_SECONDS = 180  # 每段3分钟, 保证在qwen3-asr时长限制内

# LLM 供应商优先级: SenseNova(免费) -> DeepSeek官方key
CONFIG_FILE = APPDIR / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.is_file():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def llm_providers():
    cfg = load_config()
    out = []
    sn = cfg.get("sensenova_key") or os.environ.get("SENSENOVA_API_KEY", "")
    if sn:
        out.append(("SenseNova", "https://token.sensenova.cn/v1/chat/completions",
                    sn, cfg.get("sensenova_model", "deepseek-v4-flash")))
    ds = DS_KEY
    if not ds:
        # 可选的本地兜底: 项目根或用户主目录下的 .api.key (每行 "deepseek:xxxx")
        for cand in (BASE / ".api.key", Path.home() / ".class-copilot" / "api.key"):
            try:
                for line in cand.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.lower().startswith("deepseek") or line.lower().startswith("ds"):
                        ds = line.split(":", 1)[-1].strip()
                        break
            except OSError:
                continue
            if ds:
                break
    if ds:
        out.append(("DeepSeek", DS_URL, ds, "deepseek-chat"))
    return out


def _chat_once(url: str, key: str, model: str, prompt: str, system: str, max_tokens: int,
               extra: Optional[dict] = None):
    """单次 chat 调用, 返回 (content, reasoning, finish_reason)。
    SenseNova 等模型在长输出时会只给 reasoning、不给 content, 这里把两者都取出来判断。"""
    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) +
                    [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if extra:
        body.update(extra)
    r = httpx.post(url, json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=300)
    r.raise_for_status()
    j = r.json()
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return (msg.get("content") or ""), (msg.get("reasoning") or ""), ch.get("finish_reason")


def llm_chat(prompt: str, system: str = "", max_tokens: int = 6000,
             extra: Optional[dict] = None, plain: bool = False) -> str:
    """plain=True 用于「原样校对」类任务: 给 SenseNova 推理型模型关掉 thinking,
    否则它把预算全花在 reasoning 上, content 为空(finish_reason=length)。"""
    errors = []
    for name, url, key, model in llm_providers():
        this_extra = dict(extra or {})
        if plain and name == "SenseNova":
            this_extra.setdefault("thinking", {"type": "disabled"})
        budget = max_tokens
        for attempt in range(2):
            try:
                content, reasoning, fin = _chat_once(url, key, model, prompt, system,
                                                     budget, this_extra or None)
            except Exception as e:
                errors.append(f"{name}: {e}")
                break
            if content.strip():
                return content
            if attempt == 0:
                # reasoning-only / 被 length 截断: 抬高额度重试一次
                budget = min(max(budget * 2, 4000), 8000)
                errors.append(f"{name}: 无 content(finish_reason={fin})，提升 max_tokens 到 {budget} 重试")
                continue
            if reasoning.strip():
                # 兜底: 把 reasoning 当输出返回, 由调用方从里面解析 JSON
                errors.append(f"{name}: 仍无 content(finish_reason={fin})，改用 reasoning 文本兜底")
                return reasoning
            errors.append(f"{name}: 无 content 且无 reasoning(finish_reason={fin})")
            break
    raise RuntimeError("所有LLM供应商均失败: " + " | ".join(errors))


# ---------- PPT 热词/大纲抽取 ----------

TERMS_CHUNK_CHARS = 3500
TERMS_EXTRACT_BUDGET = 300      # 单次抽取的总耗时上限(秒), 超时用已抽到的部分结果


def _parse_json_loose(txt: str) -> dict:
    """从模型输出里尽量解析出 JSON 对象(容忍 //注释、尾逗号、前后废话)。"""
    if not txt:
        return {}
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {}
    s = re.sub(r"//[^\n]*", "", m.group(0))
    s = re.sub(r",\s*(?=[}\]])", "", s)
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _salvage_json(txt: str) -> dict:
    """从被截断的输出里抢救 terms / outline 数组。"""
    if not txt:
        return {}
    out = {}
    for key in ("terms", "outline"):
        m = re.search(r'"%s"\s*:\s*\[(.+?)(?:\]|\Z)' % key, txt, re.S)
        if not m:
            continue
        body = m.group(1).rstrip().rstrip(",")
        try:
            vals = json.loads("[" + body + "]")
        except Exception:
            vals = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        out[key] = [v for v in vals if isinstance(v, str)]
    return out if (out.get("terms") or out.get("outline")) else {}


def _norm_terms(raw) -> list:
    out, seen = [], set()
    for t in raw or []:
        if not isinstance(t, str):
            continue
        s = re.sub(r"\s+", " ", t).strip().strip('"“”\'`*').strip()
        if not s or len(s) > 40:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _terms_chunks(text: str, limit: int = TERMS_CHUNK_CHARS) -> list:
    """按 --- 第N页 --- 边界切块, 每块不超过 limit 字符, 避免长 prompt 把模型 reasoning 撑爆。"""
    parts = [p for p in re.split(r"\n+(?=--- 第\d+页 ---)", text) if p.strip()]
    chunks, cur = [], ""
    for p in parts:
        if len(p) > limit:
            if cur:
                chunks.append(cur); cur = ""
            for i in range(0, len(p), limit):
                chunks.append(p[i:i + limit])
            continue
        if len(cur) + len(p) + 2 > limit and cur:
            chunks.append(cur); cur = ""
        cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


# ---------- 抽取输入: 课程名 + 文件库/笔记里所有可读文本(全部有界) ----------
TEXT_MAX_FILES = 20               # 单次最多读多少个来源
TEXT_PER_FILE_CHARS = 30000       # 单个来源最多取多少字
TEXT_TOTAL_CHARS = 60000          # 单次抽取总字数上限(约 17 个 chunk)
TEXT_PDF_PAGE_BUDGET = 80         # 单次最多解析多少页 PDF
TEXT_MAX_FILE_BYTES = 80 * 1024 * 1024   # 超过此大小的原件不提取
TEXT_TIME_BUDGET = 90             # 取文本总耗时上限(秒)


def _pdf_text(p: Path, page_budget: int, char_budget: int) -> str:
    """PDF 提正文; 页数与字数都有上限, 扫描版会返回空串。"""
    import fitz
    parts, used, pages = [], 0, 0
    doc = fitz.open(str(p))
    try:
        for page in doc:
            if pages >= page_budget or used >= char_budget:
                break
            pages += 1
            t = (page.get_text("text") or "").strip()
            if t:
                parts.append(t)
                used += len(t)
    finally:
        doc.close()
    return "\n".join(parts)[:char_budget]


def _docx_text(p: Path, char_budget: int) -> str:
    """从 docx 的 word/document.xml 提取纯文本(不引第三方依赖)。"""
    import zipfile
    from html import unescape
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    text = unescape(xml)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:char_budget]


def _extract_entry_text(key: str, meta: dict, ent: dict) -> tuple:
    """取一个文件条目的可读文本, 返回 (文本, 类型, 失败原因)。
    key 为课堂 cid 或课程 gid, 由 _store_root 决定从哪个目录读原件。"""
    kind = ent.get("kind")
    if ent.get("fid") == "ppt":
        t = (meta.get("slides_text") or "").strip()
        return (t, "ppt", None) if t else ("", "ppt", "PPT 未取到文字（扫描版/图片型）")
    if kind not in ("pdf", "docx", "md", "txt"):
        return "", kind or "该类型", f"{kind or '该类型'}暂不支持提取文本"
    p = _file_path(key, meta, ent)
    if not p or not p.is_file():
        return "", kind, "原件缺失"
    if p.stat().st_size > TEXT_MAX_FILE_BYTES:
        return "", kind, f"文件过大（{p.stat().st_size // 1024 // 1024}MB），未提取"
    if kind == "pdf":
        t = _pdf_text(p, TEXT_PDF_PAGE_BUDGET, TEXT_PER_FILE_CHARS)
        return (t, "pdf", None) if t.strip() else ("", "pdf", "PDF 未取到文字（可能是扫描版）")
    if kind == "docx":
        t = _docx_text(p, TEXT_PER_FILE_CHARS)
        return (t, "docx", None) if t.strip() else ("", "docx", "Word 未取到文字")
    t = _read_text_file(p)
    return (t, kind, None) if t.strip() else ("", kind, "文件内容为空")


def collect_course_files_text(gid: str, limit: int = 8000) -> str:
    """课程文件库的文本(供讲稿/思维导图参考), 有总字数上限。"""
    cmeta = load_course_meta(gid)
    parts, total = [], 0
    for ent in cmeta.get("files") or []:
        if not isinstance(ent, dict) or total >= limit:
            continue
        try:
            t, _kind, _reason = _extract_entry_text(gid, cmeta, ent)
        except Exception:
            continue
        if not t:
            continue
        t = t[:min(limit - total, TEXT_PER_FILE_CHARS)]
        parts.append(f"--- 文件：{ent.get('name') or '未命名'} ---\n{t}")
        total += len(t)
    return "\n\n".join(parts)


def collect_course_text(cid: str, meta: dict) -> dict:
    """汇总「课堂笔记 md + 文件库可读文本」, 返回 text/sources/skipped。
    PPT 条目沿用 meta['slides_text']; PDF/Word/md/txt 现场提取。
    文件数、单文件字数、总字数、PDF 页数、总耗时都有硬上限, 避免大文件把服务拖死。"""
    deadline = time.time() + TEXT_TIME_BUDGET
    sources, skipped, parts = [], [], []
    total = 0

    def add(name: str, kind: str, text: str) -> bool:
        nonlocal total
        t = (text or "").strip()
        if not t:
            return False
        room = TEXT_TOTAL_CHARS - total
        if room <= 0:
            skipped.append({"name": name, "reason": "已达总字数上限"})
            return False
        t = t[:min(TEXT_PER_FILE_CHARS, room)]
        sources.append({"name": name, "kind": kind, "chars": len(t)})
        parts.append(f"--- 文件：{name} ---\n{t}")
        total += len(t)
        return True

    def full(name: str) -> bool:
        if len(sources) >= TEXT_MAX_FILES:
            skipped.append({"name": name, "reason": f"超出文件数上限（最多 {TEXT_MAX_FILES} 个）"})
            return True
        if time.time() > deadline:
            skipped.append({"name": name, "reason": f"超出耗时预算（{TEXT_TIME_BUDGET} 秒）"})
            return True
        return False

    # 1) 课堂笔记(md 文件)
    try:
        for f in sorted(notes_dir(cid).glob("*.md")):
            if full(f.name):
                break
            try:
                if f.stat().st_size > TEXT_MAX_FILE_BYTES:
                    skipped.append({"name": f.name, "reason": "文件过大，未提取"})
                    continue
                if not add(f"笔记 {f.name}", "note", _read_text_file(f)):
                    skipped.append({"name": f.name, "reason": "笔记内容为空"})
            except Exception as e:
                skipped.append({"name": f.name, "reason": f"读取失败：{type(e).__name__}"})
    except OSError:
        pass

    # 2) 课堂文件库
    for ent in meta.get("files") or []:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name") or ent.get("fid") or "未命名"
        if full(name):
            continue
        try:
            t, kind, reason = _extract_entry_text(cid, meta, ent)
            if reason:
                skipped.append({"name": name, "reason": reason})
            elif not add(name, kind, t):
                skipped.append({"name": name, "reason": "文件内容为空"})
        except Exception as e:
            skipped.append({"name": name, "reason": f"解析失败：{type(e).__name__}: {e}"[:120]})

    # 3) 课程文件(仅在课堂里显式开启后参与)
    gid = meta.get("group_id")
    if meta.get("use_course_files") and gid:
        cmeta = load_course_meta(gid)
        for ent in cmeta.get("files") or []:
            if not isinstance(ent, dict):
                continue
            name = "课程文件 " + (ent.get("name") or ent.get("fid") or "未命名")
            if full(name):
                continue
            try:
                t, kind, reason = _extract_entry_text(gid, cmeta, ent)
                if reason:
                    skipped.append({"name": name, "reason": reason})
                elif not add(name, kind, t):
                    skipped.append({"name": name, "reason": "文件内容为空"})
            except Exception as e:
                skipped.append({"name": name, "reason": f"解析失败：{type(e).__name__}: {e}"[:120]})

    text = "\n\n".join(parts)
    return {"text": text, "sources": sources, "skipped": skipped, "chars": len(text)}


def _text_missing_msg(col: dict) -> str:
    """把所有来源都没取到文本的原因拼成一句可读提示。"""
    detail = "；".join(f"{s['name']}（{s['reason']}）" for s in (col.get("skipped") or [])[:8])
    msg = "本课堂没有取到可读文本，无法自动生成热词"
    if detail:
        msg += "：" + detail
    else:
        msg += "：文件库与笔记都是空的，请先上传 PDF/Word/PPT，或写课堂笔记"
    return msg + "。可手动填写热词。"


def extract_terms_outline(doc_text: str, course_name: str = ""):
    """从「课程名 + 全部可读文本」抽取 (terms, outline, error/warning)。
    失败时返回空列表 + 说明, 调用方据此决定不覆盖旧值。"""
    if not (doc_text or "").strip():
        return [], [], ("本课堂的所有文件都没有取到可读文本，无法自动生成热词，请手动填写热词")
    chunks = _terms_chunks(doc_text)
    sys = "你是课程助教，只输出JSON，不要输出其他文字。"
    ctx = f"《{course_name}》" if course_name else "本课程"
    all_terms, all_outline, errs, ok = [], [], [], 0
    t0 = time.time()
    for i, ch in enumerate(chunks, 1):
        if time.time() - t0 > TERMS_EXTRACT_BUDGET:
            errs.append(f"从第{i}段起跳过：超出 {TERMS_EXTRACT_BUDGET} 秒抽取预算")
            break
        prompt = f"""下面是{ctx}资料的第{i}/{len(chunks)}段文本（可能来自 PPT、PDF、Word 或课堂笔记）。请输出JSON：
{{"terms": ["术语1", ...]  // 本段专业术语，只放术语不放常用词
,"outline": ["第N段: ..."]  // 每段一句话概要
}}
资料文本：
{ch}"""
        try:
            parsed = {}
            last_exc = None
            # 模型可能把 max_tokens 全用在 reasoning 上导致 content 截断, 解析失败就抬高额度重试一次
            for budget in (3200, 6400):
                try:
                    # plain=True 关掉 SenseNova 的 thinking: 否则 token 全花在推理上,
                    # content 为空要走两遍重试, 慢到用户以为卡死
                    txt = llm_chat(prompt, sys, max_tokens=budget, plain=True)
                except Exception as e:
                    last_exc = e
                    continue
                parsed = _parse_json_loose(txt)
                if not parsed:
                    parsed = _salvage_json(txt)
                if parsed:
                    break
            if not parsed:
                raise ValueError(str(last_exc) if last_exc else "返回内容不是有效 JSON")
            ok += 1
            all_terms += parsed.get("terms") or []
            all_outline += [o for o in (parsed.get("outline") or []) if isinstance(o, str)]
        except Exception as e:
            errs.append(f"第{i}段: {e}")
    if ok == 0:
        return [], [], ("自动抽取热词失败：" + (" | ".join(errs)[:300] if errs else "未知原因")
                        + "。请手动填写热词")
    terms = _norm_terms(all_terms)[:40]
    warn = None
    if errs:
        warn = (f"部分页抽取失败（{ok}/{len(chunks)} 段成功），热词可能不完整："
                + " | ".join(errs)[:200])
    return terms, all_outline, warn


def apply_terms_result(meta: dict, terms, outline, err) -> dict:
    """写入抽取结果: 有失败(含部分失败)且已有热词时, 保留旧热词/大纲, 只记录原因。"""
    old = meta.get("terms") or []
    if err and old:
        meta["terms_error"] = err
    else:
        meta["terms"] = terms
        if outline:
            meta["outline"] = outline
        meta["terms_error"] = err or None
    return meta


# ---------- 课堂 AI 助手人格（集中定义，便于统一修改） ----------
# 可在 config.json 写 "assistant_persona": "..." 覆盖下面这段人格文本（优先级规则保持内置）。
ASSISTANT_PERSONA = """你是 Class Copilot 的课堂助手「小课」，人设固定如下（只影响语气与讲法，不改变事实）：
1. 可爱、亲切：像坐在旁边的同学，语气轻快、有温度；可以偶尔用「我们」「一起看看」，但不要卖萌、不堆颜文字、不说油腻套话。
2. 先给结论：第一句直接回答问题，再解释原因；必要时再给步骤或公式。
3. 务必易懂：用类比或生活化小例子说明抽象概念；术语第一次出现写成「中文（English）」，例如「梯度下降（Gradient Descent）」。
4. 诚实：不确定就直接说「我不确定」，并说明缺什么信息；绝不编造数据、结论、引用或来源。
5. 简洁：默认 3~6 句，用短段落或列表；不要整段复述字幕。"""

ASSISTANT_PRIORITY = """【事实与优先级】
1. 本课 PPT 文本与「本课字幕/转写」是事实底座，优先级最高。
2. 联网搜索结果仅在用户明确要求联网时使用，属于补充事实；引用必须给出链接，结果冲突时要说明。
3. 对话历史只用于理解指代、保持连贯，不能当作事实依据。
4. 人格设定只决定语气与表达结构，不得覆盖或改写事实。
5. 资料里没有的内容不要猜；缺失或冲突时明确说「我不确定 / 资料里没提到」。"""

app = FastAPI(title="Class Copilot")


# ---------- 基础工具 ----------

# ---------- 真实课程目录: 课程库/<课程名>/<课堂名>/ ----------
# 课堂数据(meta.json / .work / 四类资料)直接放在这里, 不再有 <cid>/ 扁平目录
# 加另一套镜像副本。cid 仍是不随改名/移动变化的稳定 id, 由目录里的 .cid 反查。
ARCHIVE_ROOT = LIB
ARCHIVE_SECTIONS = ("笔记", "参考文件", "录音原稿", "AI问答", "AI整理")
ARCHIVE_LOCK = threading.RLock()
CLASS_INDEX = {}                    # cid -> 课堂目录
CLASS_INDEX_LOCK = threading.RLock()


def _archive_safe(s, fallback="未命名"):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s or "").strip())
    s = s.strip(" .") or fallback
    return s[:80]


def _find_class_by_cid(cid: str) -> Optional[Path]:
    """在课程库里按 .cid 标记现找课堂目录(索引没就绪时的兜底)。"""
    if not cid or not ARCHIVE_ROOT.is_dir():
        return None
    for course in ARCHIVE_ROOT.iterdir():
        if not course.is_dir():
            continue
        for klass in course.iterdir():
            f = klass / ".cid"
            if not klass.is_dir() or not f.is_file():
                continue
            try:
                if f.read_text(encoding="utf-8").strip() == cid:
                    return klass
            except OSError:
                continue
    return None


def cdir(cid: str) -> Path:
    with CLASS_INDEX_LOCK:
        d = CLASS_INDEX.get(cid)
    if d and d.is_dir():
        return d
    # 索引还没建好(刚启动/刚改动磁盘)时现扫一次, 别让用户撞 404
    hit = _find_class_by_cid(cid)
    if hit:
        with CLASS_INDEX_LOCK:
            CLASS_INDEX[cid] = hit
        return hit
    d = DATA / cid              # 兼容尚未迁移 / 从备份还原的扁平目录
    if d.is_dir():
        return d
    raise HTTPException(404, "course not found")


def rebuild_class_index():
    """扫描 课程库/<课程>/<课堂>/.cid 建立反查表。标记文件让目录自描述, 索引丢了可重建。"""
    idx = {}
    if ARCHIVE_ROOT.is_dir():
        for course in ARCHIVE_ROOT.iterdir():
            if not course.is_dir():
                continue
            for klass in course.iterdir():
                if not klass.is_dir() or not (klass / ".cid").is_file():
                    continue
                try:
                    k = (klass / ".cid").read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if k:
                    idx[k] = klass
    with CLASS_INDEX_LOCK:
        CLASS_INDEX.clear()
        CLASS_INDEX.update(idx)
    return len(idx)


def iter_class_dirs():
    """遍历 (cid, 目录): 以课程库里的真实课堂目录为准, 兼容旧 data/ 里的扁平目录。"""
    seen = set()
    with CLASS_INDEX_LOCK:
        items = list(CLASS_INDEX.items())
    for cid, d in items:
        if d.is_dir():
            seen.add(str(d.resolve()))
            yield cid, d
    old = _old_data_dir()
    if not old.is_dir():
        return
    for d in sorted(old.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == LIB.name:
            continue
        if not (d / "meta.json").is_file() or str(d.resolve()) in seen:
            continue
        yield d.name, d


def ensure_class_sections(d: Path):
    for sec in ARCHIVE_SECTIONS:
        (d / sec).mkdir(parents=True, exist_ok=True)
    (d / "AI问答" / "历史").mkdir(parents=True, exist_ok=True)
    return d


def _course_name_of(gid) -> str:
    if not gid:
        return "未归属课程"
    for g in load_groups():
        if g.get("id") == gid:
            return g.get("name") or "未命名课程"
    return "未归属课程"


def class_target(cid: str, name: str, gid) -> Path:
    """按当前课程名/课堂名算目标目录(不创建); 同名时加 cid 前缀避免覆盖别的课堂。"""
    course = _archive_safe(_course_name_of(gid), "未归属课程")
    klass = _archive_safe(name, cid)
    root = ARCHIVE_ROOT / course / klass
    marker = root / ".cid"
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() != cid:
                root = ARCHIVE_ROOT / course / f"{klass}-{cid[:6]}"
        except OSError:
            pass
    return root


def register_class(cid: str, d: Path):
    try:
        (d / ".cid").write_text(cid, encoding="utf-8")
    except OSError:
        pass
    with CLASS_INDEX_LOCK:
        CLASS_INDEX[cid] = d
    ensure_class_sections(d)


def relocate_class(cid: str, name=None, gid=...):
    """课堂改名 / 换课程 / 课程改名后, 把真实目录一起搬过去并更新索引。"""
    old = cdir(cid)
    meta = load_meta(cid)
    new_name = meta.get("name") if name is None else name
    new_gid = meta.get("group_id") if gid is ... else gid
    target = class_target(cid, new_name, new_gid)
    if target.resolve() == old.resolve():
        return old
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.parent / f"{target.name}-{cid[:6]}"
    try:
        _move_path(old, target)
    except OSError as e:
        raise HTTPException(500, f"移动课堂目录失败（文件可能被占用）：{e}")
    register_class(cid, target)
    return target


def _migrate_inner_dirs(d: Path):
    """把技术目录原地改名成用户可见的四类目录(只 rename, 不复制)。"""
    for src, dst in (("files", "参考文件"), ("notes", "笔记"), ("chat", "AI问答")):
        s, t = d / src, d / dst
        if s.is_dir() and not t.exists():
            try:
                s.rename(t)
            except OSError:
                pass
    ai = d / "AI问答"
    if ai.is_dir():
        h = ai / "history.json"
        if h.is_file() and not (ai / "当前会话.json").exists():
            try:
                h.rename(ai / "当前会话.json")
            except OSError:
                pass
        a = ai / "archive"
        if a.is_dir() and not (ai / "历史").exists():
            try:
                a.rename(ai / "历史")
            except OSError:
                pass
    raw = d / "录音原稿"
    raw.mkdir(exist_ok=True)
    for f in list(d.glob("audio_s*.mp3")):
        dst = raw / f.name
        if not dst.exists():
            try:
                f.rename(dst)
            except OSError:
                pass


def _old_data_dir() -> Path:
    """旧版数据目录(升级前的 data/)。"""
    return BASE / "data"


def migrate_to_nested_dirs():
    """一次性把旧 data/<cid>/ 扁平目录搬进课程库; 幂等, 已迁移的不会再动。"""
    # 旧版「课程库」里全是副本, 且每个副本也带 .cid 标记。若不先清掉,
    # rebuild_class_index 会把副本误认成真实课堂。整个备份到废纸篓可随时找回。
    if ARCHIVE_ROOT.is_dir() and (ARCHIVE_ROOT / ".index.json").is_file():
        try:
            bak = _trash_dir("old-mirror") / "课程库"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(ARCHIVE_ROOT), str(bak))
        except OSError:
            for f in ARCHIVE_ROOT.rglob(".cid"):
                try:
                    f.unlink()
                except OSError:
                    pass
    rebuild_class_index()
    moved, failed = 0, []
    old = _old_data_dir()
    if not old.is_dir():
        return {"moved": 0, "failed": []}
    for d in sorted(old.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name == LIB.name:
            continue
        if not (d / "meta.json").is_file():
            continue
        if not any((d / x).is_dir() for x in ("work", ".work", "参考文件", "notes", "files")):
            continue                      # 不是课堂目录, 不动
        cid = d.name
        with CLASS_INDEX_LOCK:
            done = cid in CLASS_INDEX
        if done:
            continue
        try:
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed.append(cid)
            continue
        target = class_target(cid, m.get("name") or cid, m.get("group_id"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.parent / f"{target.name}-{cid[:6]}"
        try:
            _move_path(d, target)
            _migrate_inner_dirs(target)
            register_class(cid, target)
            moved += 1
        except OSError:
            failed.append(cid)
    return {"moved": moved, "failed": failed}


def _move_path(src: Path, dst: Path) -> bool:
    """移动文件/目录: 优先原子重命名(同盘), 失败再退回 shutil.move。"""
    try:
        os.rename(str(src), str(dst))
        return True
    except OSError:
        try:
            shutil.move(str(src), str(dst))
            return True
        except Exception:
            return False


def _promote_data_root() -> list:
    """把旧 data/ 里的内容搬到项目根: 课程库露出来, 其余全部用 . 前缀隐藏。"""
    old = _old_data_dir()
    notes = []
    if not old.is_dir():
        return notes
    pairs = [(LIB.name, LIB), (".courses", DATA / ".courses"), (".covers", DATA / ".covers"),
             (".trash", DATA / ".trash"), ("groups.json", DATA / ".groups.json"),
             ("ui_state.json", DATA / ".ui_state.json")]
    keep = DATA / ".data_old"
    for name, dst in pairs:
        s = old / name
        if not s.exists():
            continue
        if dst.exists() and not s.is_dir():
            # 文件冲突: 旧 data/ 里的才是权威数据, 把新的挪到 .data_old 后覆盖
            keep.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(str(dst), str(keep / (dst.name + ".bak")))
            except OSError:
                pass
            try:
                os.replace(str(s), str(dst))
            except OSError:
                _move_path(s, keep / s.name)
        elif dst.exists():
            dst.mkdir(parents=True, exist_ok=True)
            for sub in s.iterdir():
                t = dst / sub.name
                if not t.exists() and not _move_path(sub, t):
                    notes.append(f"data/{name}/{sub.name} 移动失败")
            shutil.rmtree(s, ignore_errors=True)
        else:
            if not _move_path(s, dst):
                notes.append(f"data/{name} 移动失败")
                continue
        notes.append(f"data/{name} -> {dst.name}")
    left = list(old.iterdir())
    if left:
        keep.mkdir(parents=True, exist_ok=True)
        for p in left:
            if not _move_path(p, keep / p.name):
                notes.append(f"data/{p.name} 移动失败")
        notes.append(f"data/ 其余 {len(left)} 项 -> .data_old")
    try:
        os.rmdir(str(old))            # 空目录直接删
    except OSError:
        shutil.rmtree(old, ignore_errors=True)
    return notes


def _tidy_class_dir(cid: str, d: Path, meta: dict) -> list:
    """把课堂根目录里散落的技术文件归位, 并把参考文件改成可读的原始文件名。"""
    notes = []
    files = d / "参考文件"
    files.mkdir(parents=True, exist_ok=True)
    pptprev = files / ".preview" / "ppt"
    pptprev.mkdir(parents=True, exist_ok=True)

    # AI 产物(讲课复现/思维导图/实时要点)从「笔记」挪到「AI整理」:
    # 「笔记」只留用户自己写的 md
    ai_dir = d / "AI整理"
    ndir = d / "笔记"
    moved_ai = []
    for nm in ("讲课复现.md", "思维导图.md", "实时要点.md"):
        src = ndir / nm
        if src.is_file():
            ai_dir.mkdir(parents=True, exist_ok=True)
            dst = ai_dir / nm
            try:
                if dst.exists():
                    dst.unlink()
                os.replace(str(src), str(dst))
                moved_ai.append(nm)
            except OSError:
                pass
    if moved_ai:
        notes.append("AI产物 -> AI整理: " + ", ".join(moved_ai))

    # 录音中间文件 work/ -> .work/(隐藏)
    w = d / "work"
    if w.is_dir():
        nw = d / ".work"
        if nw.exists():
            for sub in w.iterdir():
                t = nw / sub.name
                if not t.exists():
                    os.rename(str(sub), str(t))
            shutil.rmtree(w, ignore_errors=True)
        else:
            w.rename(nw)
        notes.append("work -> .work")

    # 整页切图 pages/ -> 参考文件/.preview/ppt/pages/
    pg = d / "pages"
    if pg.is_dir():
        tgt_dir = pptprev / "pages"
        tgt_dir.mkdir(parents=True, exist_ok=True)
        for f in pg.iterdir():
            t = tgt_dir / f.name
            if not t.exists():
                os.rename(str(f), str(t))
        shutil.rmtree(pg, ignore_errors=True)
        notes.append("pages -> 参考文件/.preview/ppt/pages")

    # PPT 原件 slides.* -> 参考文件/<原名>
    ent = _file_entry(meta, "ppt")
    for name in ("slides.pptx", "slides.pdf", "slides.ppt"):
        s = d / name
        if not s.is_file():
            continue
        orig = (ent or {}).get("name") or name
        new = _safe_stored_name(orig, s.suffix.lower(), files)
        s.rename(files / new)
        if ent is not None:
            ent["stored"] = new
            ent["name"] = new
        notes.append(f"{name} -> 参考文件/{new}")

    # 预览用 PDF slides_view.pdf -> 参考文件/.preview/ppt/view.pdf
    sv = d / "slides_view.pdf"
    if sv.is_file():
        t = pptprev / "view.pdf"
        if t.exists():
            sv.unlink()
        else:
            os.rename(str(sv), str(t))
        notes.append("slides_view.pdf -> 参考文件/.preview/ppt/view.pdf")

    # 整堂音频 audio.mp3 -> 录音原稿/课堂全程.mp3
    am = d / "audio.mp3"
    if am.is_file():
        raw = d / "录音原稿"
        raw.mkdir(parents=True, exist_ok=True)
        t = raw / "课堂全程.mp3"
        if t.exists():
            am.unlink()
        else:
            os.rename(str(am), str(t))
        notes.append("audio.mp3 -> 录音原稿/课堂全程.mp3")

    # 参考文件/<内部id>.pdf -> 参考文件/<原始文件名>.pdf
    for e in meta.get("files") or []:
        if not isinstance(e, dict) or e.get("fid") == "ppt" or not e.get("stored"):
            continue
        p = files / e["stored"]
        if not p.is_file():
            continue
        want = _safe_stored_name(e.get("name") or e["stored"], p.suffix.lower(),
                                  files, keep=e["stored"])
        if want != e["stored"]:
            p.rename(files / want)
            e["stored"] = want
            notes.append(f"参考文件/{e['stored']} -> {want}")
    return notes


def migrate_layout() -> dict:
    """数据搬到项目根 + 课堂内文件归位 + 目录按归属归位; 幂等, 可反复执行。"""
    rep = {"promoted": [], "tidied": 0, "items": [], "errors": []}
    try:
        rep["promoted"] = _promote_data_root()
    except Exception as e:
        rep["errors"].append(f"promote: {type(e).__name__}: {e}")
    try:
        migrate_to_nested_dirs()
    except Exception as e:
        rep["errors"].append(f"nested: {type(e).__name__}: {e}")
    rebuild_class_index()
    # 课程列表要先就位, 否则算不出课堂该待在哪个课程目录下
    try:
        ensure_groups()
    except Exception as e:
        rep["errors"].append(f"groups: {type(e).__name__}: {e}")
    rebuild_class_index()
    for cid, d in list(iter_class_dirs()):
        try:
            meta = load_meta(cid)
            items = _tidy_class_dir(cid, d, meta)
            # 目录位置与归属不一致(例如课程改名/归属恢复过) -> 搬回正确位置
            want = class_target(cid, meta.get("name") or cid, meta.get("group_id"))
            try:
                same = want.resolve() == d.resolve()
            except OSError:
                same = True
            if not same and not want.exists():
                want.parent.mkdir(parents=True, exist_ok=True)
                if _move_path(d, want):
                    register_class(cid, want)
                    items.append(f"目录归位 -> {want.parent.name}/{want.name}")
            if items:
                save_meta(cid, meta)
                rep["tidied"] += 1
                rep["items"].extend(f"{cid}: {i}" for i in items)
        except Exception as e:
            rep["errors"].append(f"{cid}: {type(e).__name__}: {e}")
    return rep


META_LOCK = threading.Lock()


def _dedupe_sessions(meta: dict) -> dict:
    """历史版本在多进程/重复点击下可能写入相同 sid，这里合并去重。"""
    out, by_sid = [], {}
    for s in meta.get("sessions") or []:
        sid = s.get("sid")
        if sid in by_sid:
            prev = by_sid[sid]
            if s.get("ended") and not prev.get("ended"):
                prev["ended"] = s["ended"]
            if s.get("started") and not prev.get("started"):
                prev["started"] = s["started"]
            if s.get("stale"):
                prev["stale"] = True
        else:
            by_sid[sid] = s
            out.append(s)
    meta["sessions"] = out
    return meta


OBJ_REPR_RE = re.compile(r"\[?<wave\.Wave_write object at 0x[0-9A-Fa-f]+>\]?\s*")


def clean_obj_repr(text: str) -> str:
    """移除历史 bug 写入转写稿的 wave 对象 repr; 只匹配这一类明确对象, 不动正常正文。"""
    if not text or "<wave.Wave_write object at" not in text:
        return text
    return OBJ_REPR_RE.sub("", text)


def load_meta(cid: str) -> dict:
    meta = json.loads((cdir(cid) / "meta.json").read_text(encoding="utf-8"))
    meta = _dedupe_sessions(meta)
    for key in ("transcript", "live_transcript"):
        if meta.get(key):
            meta[key] = clean_obj_repr(meta[key])
    return meta


def _atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_meta(cid: str, meta: dict):
    _atomic_write_json(cdir(cid) / "meta.json", meta)
    # 课程目录镜像: meta 变化 = 可能有新的转写/文件/问答, 顺手同步(只在内容变化时才真复制)
    try:
        _archive_sync(cid, meta=meta)
    except Exception:
        pass


def fmt_ts(sec: int) -> str:
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ---------- 转写稿时间戳: 绝对时间 -> 本次课内的相对时间 ----------
def _rel_ts_conv_text(txt: str, sessions: dict) -> tuple[str, int]:
    """把 live_transcript 里 [HH:MM] 的绝对时间换成 [mm:ss] 相对时间(每次课各自从 00:00 起)。"""
    if not txt:
        return txt, 0
    parts = re.split(r"(【第(\d+)次上课】)", txt)
    out, n = [], 0
    cur_start = None
    for seg in parts:
        m = re.fullmatch(r"【第(\d+)次上课】", seg or "")
        if m:
            st = sessions.get(int(m.group(1)))
            cur_start = None
            if st:
                try:
                    cur_start = datetime.fromisoformat(st)
                except ValueError:
                    pass
            out.append(seg)
            continue
        if cur_start and seg:
            def repl(mm):
                nonlocal n
                hh, mi = int(mm.group(1)), int(mm.group(2))
                d = (hh * 60 + mi) - (cur_start.hour * 60 + cur_start.minute)
                if d < 0:
                    return mm.group(0)
                n += 1
                return "[%02d:%02d]" % (d // 60, d % 60)
            seg = re.sub(r"\[(\d{1,2}):(\d{2})\]", repl, seg)
        out.append(seg)
    return "".join(out), n


def _convert_rel_timestamps() -> int:
    """启动时把(旧版本写入的)绝对时间戳增量换算成相对时间; 已换算过的部分用 ts_conv_upto 记住。"""
    total = 0
    for cid, d in list(iter_class_dirs()):
        try:
            meta = load_meta(cid)
            sess = {s.get("label"): s.get("started") for s in (meta.get("sessions") or [])
                    if s.get("started")}
            if not sess:
                continue
            upto = int(meta.get("ts_conv_upto") or 0)
            changed = False
            for key in ("live_transcript", "transcript", "reviewed_transcript"):
                txt = meta.get(key)
                if not isinstance(txt, str) or not txt:
                    continue
                head, tail = (txt[:upto], txt[upto:]) if 0 < upto <= len(txt) else ("", txt)
                new_tail, n = _rel_ts_conv_text(tail, sess)
                if n:
                    meta[key] = head + new_tail
                    changed = True
                    total += n
            if changed:
                meta["ts_mode"] = "relative"
                meta["ts_conv_upto"] = len(meta.get("live_transcript") or "")
                save_meta(cid, meta)
        except Exception:
            continue
    if total:
        print(f"[ts] 换算 {total} 个时间戳为相对时间", flush=True)
    return total


# ---------- 就地刷新可读派生文本 ----------
# 原件本来就在四类真实目录里(参考文件/笔记/录音原稿/AI问答), 这里只补纯文本与
# Markdown 视图, 不再复制任何文件到另一套目录树。
def _archive_class_dir(cid: str, meta: dict) -> Path:
    return ensure_class_sections(cdir(cid))


def _archive_write_text(path: Path, text: str):
    text = text or ""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _archive_copy(src: Optional[Path], dst: Path):
    if not src or not src.is_file() or src.stat().st_size > 60 * 1024 * 1024:
        return
    try:
        st = src.stat()
        if dst.is_file() and dst.stat().st_size == st.st_size and abs(dst.stat().st_mtime - st.st_mtime) < 2:
            return
        shutil.copy2(str(src), str(dst))
    except OSError:
        pass


def _archive_light(cid: str, meta: dict) -> Path:
    """只写文本(录音原稿 / 笔记), 供 save_meta 高频调用。"""
    with ARCHIVE_LOCK:
        root = _archive_class_dir(cid, meta)
        raw = root / "录音原稿"
        _archive_write_text(raw / "实时转写.txt", str(meta.get("live_transcript") or ""))
        _archive_write_text(raw / "整理后转写.txt", str(meta.get("transcript") or ""))
        if meta.get("reviewed_transcript"):
            _archive_write_text(raw / "AI复审稿.md", str(meta["reviewed_transcript"]))
        # AI 产物不是用户写的笔记, 单独放「AI整理」, 不混进「笔记」
        ai = root / "AI整理"
        ai.mkdir(parents=True, exist_ok=True)
        _archive_write_text(ai / "讲课复现.md", str(meta.get("notes_md") or ""))
        _archive_write_text(ai / "思维导图.md", str(meta.get("mindmap_md") or ""))
        _archive_write_text(ai / "实时要点.md", str(meta.get("live_notes") or ""))
        return root


def _archive_chat_md(p: Path) -> str:
    """把问答 JSON 转成可读的 Markdown 留档文本。"""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        msgs = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(msgs, list) or not msgs:
            return ""
        out = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            who = "我" if m.get("role") == "user" else "AI"
            out.append(f"### {who}\n\n{m.get('content') or ''}\n")
            if m.get("notice"):
                out.append(f"> 提示：{m['notice']}\n")
            for s in m.get("sources") or []:
                if isinstance(s, dict):
                    out.append(f"- [{s.get('title') or '来源'}]({s.get('url') or ''})\n")
        return "\n".join(out)
    except (OSError, json.JSONDecodeError):
        return ""


def _archive_sync(cid: str, sections=None, meta=None) -> dict:
    """就地刷新可读派生文本。四类目录就是真实存储, 这里不复制任何文件。"""
    with ARCHIVE_LOCK:
        meta = meta or load_meta(cid)
        root = _archive_light(cid, meta)
        want = set(sections or ARCHIVE_SECTIONS)
        if "参考文件" in want:
            lines = ["# 参考文件清单", "", "| 名称 | 磁盘文件名 |", "| --- | --- |"]
            for e in meta.get("files") or []:
                if isinstance(e, dict):
                    lines.append("| {} | `{}` |".format(
                        str(e.get("name") or e.get("fid") or "").replace("|", "\\|"),
                        str(e.get("stored") or "")))
            _archive_write_text(root / "参考文件" / "文件清单.md", "\n".join(lines) + "\n")
        if "AI问答" in want:
            q = root / "AI问答"
            cur = q / "当前会话.json"
            if cur.is_file():
                _archive_write_text(q / "当前会话.md", _archive_chat_md(cur))
            hist = q / "历史"
            if hist.is_dir():
                for f in hist.glob("*.json"):
                    _archive_write_text(hist / (f.stem + ".md"), _archive_chat_md(f))
        return {"path": str(root.relative_to(DATA)).replace("\\", "/"),
                "course": root.parent.name, "class": root.name}


def _archive_sync_all() -> dict:
    done, failed = 0, 0
    for cid, _d in iter_class_dirs():
        try:
            _archive_sync(cid)
            done += 1
        except Exception:
            failed += 1
    return {"ok": True, "classes": done, "failed": failed}


def _archive_tree() -> dict:
    """给前端「课程目录」弹窗用的树。"""
    if not ARCHIVE_ROOT.is_dir():
        return {"root": str(ARCHIVE_ROOT.relative_to(DATA)).replace("\\", "/"), "courses": []}
    courses = []
    try:
        course_dirs = sorted(x for x in ARCHIVE_ROOT.iterdir() if x.is_dir())
    except OSError:
        course_dirs = []
    for cd in course_dirs:
        classes = []
        try:
            klass_dirs = sorted(x for x in cd.iterdir() if x.is_dir())
        except OSError:
            continue
        for kd in klass_dirs:
            secs = []
            for sec in ARCHIVE_SECTIONS:
                sp = kd / sec
                try:
                    n = len([f for f in sp.rglob("*") if f.is_file()]) if sp.is_dir() else 0
                except OSError:
                    n = 0
                secs.append({"name": sec, "count": n})
            classes.append({"name": kd.name, "sections": secs})
        courses.append({"name": cd.name, "classes": classes})
    return {"root": str(ARCHIVE_ROOT.relative_to(DATA)).replace("\\", "/"), "courses": courses}


# ---------- 课程(分组) ----------
# 数据模型: .groups.json 保存课程列表; 每个课堂的 meta.json 用 group_id 指向课程。
# group_id 为 None/"" 表示该课堂不归属任何课程, 直接挂在主界面。

GROUPS_FILE = DATA / ".groups.json"
DEFAULT_GROUP = "g_default"
DEFAULT_GROUP_NAME = "默认课程"
TRASH = DATA / ".trash"
COVERS = DATA / ".covers"
COVER_PRESETS = {"p1", "p2", "p3", "p4", "p5", "p6"}
GROUPS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_groups_raw() -> dict:
    try:
        raw = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_groups() -> list:
    groups = _read_groups_raw().get("groups")
    return [g for g in groups if isinstance(g, dict) and g.get("id")] if isinstance(groups, list) else []


def _group_ids() -> set:
    return {g["id"] for g in load_groups()}


def ensure_groups(meta_only_migration: bool = True):
    """确保 groups.json 存在; 把旧课堂(从未有过 group_id 字段)归入默认课程。
    - 幂等: 已归属的课堂不会被再次改写, 显式置空(null)的课堂保持"不归属"状态。
    - 默认课程 g_default 允许被用户删除; 删除后写 default_deleted 标记, 不再自动重建,
      否则用户看到的「删不掉/删了又回来」就会一直发生。
    - 不移动任何课堂目录, 只原子写 meta.json / groups.json。
    """
    with GROUPS_LOCK:
        raw = _read_groups_raw()
        groups = [g for g in raw.get("groups", [])
                  if isinstance(g, dict) and g.get("id")] if isinstance(raw.get("groups"), list) else []
        changed = not GROUPS_FILE.is_file()
        deleted_default = bool(raw.get("default_deleted"))
        ids = {g["id"] for g in groups}
        if DEFAULT_GROUP not in ids and not deleted_default:
            groups.insert(0, {"id": DEFAULT_GROUP, "name": DEFAULT_GROUP_NAME,
                              "created": _now_iso(), "order": 0})
            changed = True
        for i, g in enumerate(groups):
            if not isinstance(g.get("order"), int):
                g["order"] = i
                changed = True
            if not g.get("name"):
                g["name"] = DEFAULT_GROUP_NAME if g["id"] == DEFAULT_GROUP else "未命名课程"
                changed = True
        ids = {g["id"] for g in groups}
        for i, g in enumerate(sorted(groups, key=lambda x: x.get("order", 0))):
            if g.get("order") != i:
                g["order"] = i
                changed = True
        if changed or not GROUPS_FILE.is_file():
            out = {"version": 1, "groups": groups}
            if deleted_default:
                out["default_deleted"] = True
            _atomic_write_json(GROUPS_FILE, out)
        if not meta_only_migration or not DATA.is_dir():
            return
        fallback = DEFAULT_GROUP if DEFAULT_GROUP in ids else None
        for _cid, d in iter_class_dirs():
            if not (d / "meta.json").is_file():
                continue
            try:
                m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            gid = m.get("group_id")
            if "group_id" not in m:
                # 从旧版本升级: 首次补上归属
                m["group_id"] = fallback
            elif gid and gid not in ids:
                # 指向已删除/无效课程: 有默认课程就退回, 没有则挂到主界面
                m["group_id"] = fallback
            else:
                continue
            _atomic_write_json(d / "meta.json", m)


def _next_group_order(groups: list) -> int:
    return max([g.get("order", 0) for g in groups] + [0]) + 1


def _trash_dir(tag: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = TRASH / f"{stamp}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_summary(m: dict) -> dict:
    out = {k: m.get(k) for k in ("id", "name", "created", "status", "has_ppt",
                                 "duration", "terms", "progress", "cover",
                                 "page_bg", "page_bg_img")}
    out["group_id"] = m.get("group_id")
    return out


def _valid_cover(cv):
    """卡片封面: None=默认样式; {t:"preset",v:"p1"} 预设; {t:"img",u:"/api/cover/xxx.jpg"} 图片."""
    if cv is None:
        return None
    if not isinstance(cv, dict):
        raise HTTPException(400, "cover 格式错误")
    if cv.get("t") == "preset":
        if cv.get("v") not in COVER_PRESETS:
            raise HTTPException(400, "未知的背景预设")
        return {"t": "preset", "v": cv["v"]}
    if cv.get("t") == "img":
        u = cv.get("u")
        if not isinstance(u, str) or not re.match(r"^/api/cover/[0-9a-f]{32}\.jpg$", u):
            raise HTTPException(400, "cover 图片地址无效")
        return {"t": "img", "u": u}
    raise HTTPException(400, "cover 类型无效")


# ---------- DeepSeek ----------

# ---------- PPT / PDF 提取 ----------

def split_slide_pages(pdf_path: Path, out_dir: Path, url_base: str) -> list:
    """按幻灯片边框矩形切分讲义PDF(不切字, 保留白边); 无边框则整页渲染"""
    import fitz
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    urls = []
    idx = 0
    for pno in range(len(doc)):
        page = doc[pno]
        W, H = page.rect.width, page.rect.height
        boxes = []
        for d in page.get_drawings():
            r = d["rect"]
            if (W*0.3 < r.width < W*0.98) and (H*0.2 < r.height < H*0.9):
                r = fitz.Rect(r)
                dup = False
                for u in boxes:
                    inter = r & u
                    if not inter.is_empty and inter.get_area() > 0.7 * min(r.get_area(), u.get_area()):
                        dup = True
                        if r.get_area() > u.get_area():
                            boxes[boxes.index(u)] = r
                        break
                if not dup:
                    boxes.append(r)
        # 只检测到1个框(或0个)说明是单页版式, 整页渲染, 避免误裁
        if len(boxes) < 2:
            boxes = [page.rect]
        boxes.sort(key=lambda r: (round(r.y0 / (H*0.25)), r.x0))
        # 每张幻灯片向外扩一圈白边(最多30pt), 但不越过相邻幻灯片
        PADMAX = 10
        padded = []
        for i, b in enumerate(boxes):
            x0, y0, x1, y1 = b.x0, b.y0, b.x1, b.y1
            lim = 0
            for j, o in enumerate(boxes):
                if j != i and o.x1 <= b.x0 + 1:
                    lim = max(lim, o.x1)
            x0p = max(x0 - PADMAX, lim, 0)
            lim = W
            for j, o in enumerate(boxes):
                if j != i and o.x0 >= b.x1 - 1:
                    lim = min(lim, o.x0)
            x1p = min(x1 + PADMAX, lim, W)
            lim = 0
            for j, o in enumerate(boxes):
                if j != i and o.y1 <= b.y0 + 1:
                    lim = max(lim, o.y1)
            y0p = max(y0 - PADMAX, lim, 0)
            lim = H
            for j, o in enumerate(boxes):
                if j != i and o.y0 >= b.y1 - 1:
                    lim = min(lim, o.y0)
            y1p = min(y1 + PADMAX, lim, H)
            padded.append(fitz.Rect(x0p, y0p, x1p, y1p))
        for b in padded:
            clip = fitz.Rect(max(0, b.x0), max(0, b.y0),
                             min(W, b.x1), min(H, b.y1))
            idx += 1
            pix = page.get_pixmap(dpi=150, clip=clip)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            p = out_dir / f"slide_{idx:02d}.jpg"
            img.save(p, quality=88)
            urls.append(f"{url_base}/page/{idx:02d}.jpg")
    doc.close()
    return urls


def get_page_urls(cid: str) -> list:
    # 整页切图属于预览缓存, 放在 参考文件/.preview/ppt/pages/
    prev = _files_dir(cid) / ".preview" / "ppt"
    pages_dir = prev / "pages"
    view = prev / "view.pdf"
    if not pages_dir.is_dir() and view.is_file():
        try:
            return split_slide_pages(view, pages_dir, f"/api/courses/{cid}")
        except Exception:
            return []
    if pages_dir.is_dir():
        return [f"/api/courses/{cid}/page/{p.name.split('_')[1].split('.')[0]}.jpg"
                for p in sorted(pages_dir.glob("slide_*.jpg"))]
    return []


def extract_pptx(path: Path) -> list:
    from pptx import Presentation
    prs = Presentation(str(path))
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
        slides.append({"idx": i, "text": "\n".join(parts)[:1500]})
    return slides


def extract_pdf(path: Path) -> list:
    from pypdf import PdfReader
    rd = PdfReader(str(path))
    return [{"idx": i + 1, "text": (p.extract_text() or "")[:1500]}
            for i, p in enumerate(rd.pages)]


# ---------- 课堂文件库 ----------
# 数据: meta["files"] = [{fid,name,kind,size,mtime,source,stored?}]
# 原件: data/<cid>/files/<fid><ext>; PPT 为特殊条目 fid="ppt", 原件是 slides.pdf/pptx
# 预览缓存: data/<cid>/files/.preview/<fid>/ (整页 jpg + 转换出的 view.pdf + layout.json)

FILES_MAX_BYTES = 300 * 1024 * 1024
FILE_KIND_BY_EXT = {
    ".pdf": "pdf", ".pptx": "pptx", ".ppt": "pptx",
    ".docx": "docx", ".doc": "docx", ".xlsx": "xlsx", ".xls": "xlsx",
    ".md": "md", ".markdown": "md", ".txt": "txt",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio",
    ".ogg": "audio", ".flac": "audio",
    ".mp4": "video", ".webm": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
}
PAGED_KINDS = {"pdf", "ppt", "pptx", "docx", "xlsx"}
SOFFICE_LOCK = threading.Lock()

# 课程文件库: 每个课程(groups.json 里的分组)一个独立目录, 与课堂 data/<cid>/ 平级但互不相干
# 目录: data/.courses/<gid>/{meta.json, files/...}, 预览缓存同课堂: files/.preview/<fid>/
COURSE_FILES_ROOT = DATA / ".courses"


def _course_dir(gid: str, create: bool = False) -> Path:
    d = COURSE_FILES_ROOT / gid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _course_meta_path(gid: str) -> Path:
    return _course_dir(gid) / "meta.json"


def load_course_meta(gid: str) -> dict:
    try:
        raw = json.loads(_course_meta_path(gid).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw.get("files"), list):
        raw["files"] = []
    return raw


def save_course_meta(gid: str, meta: dict):
    _atomic_write_json(_course_meta_path(gid), meta)


def course_files_count(gid: str) -> int:
    return len([f for f in load_course_meta(gid).get("files") or []
                if isinstance(f, dict) and f.get("fid")])


def _store_root(cid: str) -> Path:
    """课堂文件库根是课堂目录本身; 课程文件库根是 .courses/<gid>。
    课堂 cid 从不用 g_ 前缀, 课程 gid 固定是 g_ 前缀, 因此不会混淆。"""
    if cid.startswith("g_") or cid == DEFAULT_GROUP:
        return _course_dir(cid, create=True)
    return cdir(cid)


def _files_dir(cid: str) -> Path:
    # 课堂原件直接存在用户可见的「参考文件」里; 课程级公共文件库仍是隐藏的 .courses/<gid>/files
    is_group = cid.startswith("g_") or cid == DEFAULT_GROUP
    d = _store_root(cid) / ("files" if is_group else "参考文件")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_stored_name(name: str, ext: str, folder: Path, keep: str = "") -> str:
    """把用户原始文件名变成磁盘存储名: 可读、合法、不与已有文件冲突。"""
    base = Path(name or "").stem
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .") or "文件"
    ext = ext if ext and len(ext) <= 10 else ""
    cand = base + ext
    i = 2
    while (folder / cand).exists() and cand != keep:
        cand = f"{base}({i}){ext}"
        i += 1
    return cand


def _file_kind(name: str) -> str:
    return FILE_KIND_BY_EXT.get(Path(name or "").suffix.lower(), "other")


def _file_entry(meta: dict, fid: str):
    for f in meta.get("files") or []:
        if isinstance(f, dict) and f.get("fid") == fid:
            return f
    return None


def _preview_dir(cid: str, fid: str, create: bool = True) -> Path:
    d = _files_dir(cid) / ".preview" / fid
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _ppt_source(cid: str, meta: Optional[dict] = None) -> Optional[Path]:
    """PPT 原件: 参考文件/ 下 meta 里 ppt 条目指向的文件; 兼容旧版 slides.*。"""
    meta = meta if meta is not None else load_meta(cid)
    ent = _file_entry(meta, "ppt")
    if ent and ent.get("stored"):
        p = _files_dir(cid) / ent["stored"]
        if p.is_file():
            return p
    for name in ("slides.pptx", "slides.pdf"):
        for base in (_files_dir(cid), cdir(cid)):
            p = base / name
            if p.is_file():
                return p
    return None


def _ppt_entry_source(cid: str, meta: dict) -> Optional[Path]:
    """PPT 条目的原始文件（用于阅读/转换）。"""
    return _ppt_source(cid, meta)


def ensure_file_entries(cid: str, meta: dict) -> bool:
    """老课堂零迁移: 只补一个 PPT 特殊条目, 不改动 slides/slides_text/pages。"""
    changed = False
    if not isinstance(meta.get("files"), list):
        meta["files"] = []
        changed = True
    src = _ppt_source(cid)
    if not src:
        return changed
    ent = _file_entry(meta, "ppt")
    if not ent:
        st = src.stat()
        meta["files"].insert(0, {"fid": "ppt", "name": meta.get("ppt_file") or src.name,
                                 "kind": "ppt", "size": st.st_size,
                                 "mtime": int(st.st_mtime), "source": "ppt"})
        changed = True
    else:
        st = src.stat()
        if ent.get("size") != st.st_size:
            ent["size"] = st.st_size
            ent["mtime"] = int(st.st_mtime)
            changed = True
    return changed


def _soffice_exe() -> str:
    return shutil.which("soffice") or r"C:\Program Files\LibreOffice\program\soffice.exe"


def _lo_profile(fresh: bool = False) -> str:
    """LibreOffice 用户配置放项目内, 避免写到 C 盘用户目录。
    fresh=True 用一次性配置目录, 规避上一次异常退出留下的锁/崩溃状态。"""
    root = BASE / ".cache" / "lo"
    d = (root / ("tmp_" + uuid.uuid4().hex[:8])) if fresh else (root / "app")
    d.mkdir(parents=True, exist_ok=True)
    return "file:///" + d.as_posix()


# 最近一次转换失败原因, 供 info/page 接口回给前端显示(而不是静默返回 0 页)
CONVERT_ERRORS: dict = {}


def _soffice_convert(src: Path, outdir: Path, profile: str, timeout: int = 150):
    """调用 soffice 转 PDF, 返回 (pdf路径, 错误说明)。"""
    try:
        r = run_quiet([_soffice_exe(), "-env:UserInstallation=" + profile,
                       "--headless", "--norestore", "--nologo",
                       "--convert-to", "pdf", "--outdir", str(outdir), str(src)],
                      capture_output=True, timeout=timeout)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    out = outdir / (src.stem + ".pdf")
    if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
        return out, ""
    msg = ((r.stderr or b"") + b" " + (r.stdout or b"")).decode("utf-8", "ignore").strip()
    return None, f"soffice 返回码 {r.returncode}" + (f"：{msg[-300:]}" if msg else "，未生成 PDF")


def _convert_to_pdf(cid: str, fid: str, src: Path) -> Optional[Path]:
    """pptx/docx/xlsx -> 预览用 PDF, 结果缓存在 files/.preview/<fid>/view.pdf。
    首次失败时用全新配置目录重试一次, 仍失败则记录原因(不静默)。"""
    if not src.is_file():
        return None
    pdir = _preview_dir(cid, fid)
    out = pdir / "view.pdf"
    try:
        if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
            return out
    except OSError:
        pass
    errs = []
    for fresh in (False, True):
        conv = pdir / ("conv" if not fresh else "conv_retry")
        prof = _lo_profile(fresh)
        try:
            shutil.rmtree(conv, ignore_errors=True)
            conv.mkdir(parents=True, exist_ok=True)
            with SOFFICE_LOCK:
                produced, err = _soffice_convert(src, conv, prof)
            if produced:
                produced.replace(out)
                CONVERT_ERRORS.pop(fid, None)
                return out
            errs.append(("重试" if fresh else "首次") + "：" + err)
        except Exception as e:
            errs.append(("重试" if fresh else "首次") + f"：{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(conv, ignore_errors=True)
            if fresh:
                shutil.rmtree(Path(prof[len("file:///"):]), ignore_errors=True)
    if not Path(_soffice_exe()).is_file():
        msg = "本机未找到 LibreOffice（soffice），无法预览该格式；安装 LibreOffice 后可重试"
    else:
        msg = "转换失败：" + "；".join(errs)
    CONVERT_ERRORS[fid] = msg
    print(f"[preview] {cid}/{fid} 转换失败: {msg}", flush=True)
    return None


def _file_path(cid: str, meta: dict, entry: dict) -> Optional[Path]:
    if entry.get("fid") == "ppt":
        return _ppt_entry_source(cid, meta)
    stored = entry.get("stored")
    if not stored:
        return None
    return _files_dir(cid) / stored


def _entry_pdf(cid: str, meta: dict, entry: dict) -> Optional[Path]:
    """拿到该文件可用于按页渲染的 PDF; 非分页格式返回 None。"""
    kind = entry.get("kind")
    if kind == "ppt":
        view = _preview_dir(cid, "ppt") / "view.pdf"
        if view.is_file():
            return view
        src = _ppt_entry_source(cid, meta)
        if src and src.suffix.lower() == ".pdf":
            return src
        return _convert_to_pdf(cid, "ppt", src) if src else None
    if kind not in PAGED_KINDS:
        return None
    src = _file_path(cid, meta, entry)
    if kind == "pdf":
        return src if src and src.is_file() else None
    return _convert_to_pdf(cid, entry["fid"], src) if src else None


def _pdf_count(pdf: Optional[Path]) -> int:
    if not pdf:
        return 0
    try:
        import fitz
        doc = fitz.open(str(pdf))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def _render_page_jpg(cid: str, meta: dict, entry: dict, n: int, seg: int = 0,
                     cuts: Union[None, list, dict] = None) -> Optional[Path]:
    """整页渲染(一 PDF 页 = 一个页块); seg>0 时按 rows+cols 网格裁出第 seg 段。
    seg 编号按行主序: 先渲染第一行所有 cols+1 个块, 再第二行..."""
    pdf = _entry_pdf(cid, meta, entry)
    if not pdf:
        return None
    p = _preview_dir(cid, entry["fid"]) / f"page_{n:03d}_{seg}.jpg"
    if p.is_file():
        return p
    try:
        import fitz
        from PIL import Image
        doc = fitz.open(str(pdf))
        if n < 1 or n > len(doc):
            doc.close()
            return None
        page = doc[n - 1]
        clip = page.rect
        if seg and cuts:
            rows, cols, cells, pad_pt = [], [], None, 0.0
            if isinstance(cuts, dict):
                rows = [float(r) for r in (cuts.get("rows") or [])]
                cols = [float(c) for c in (cuts.get("cols") or [])]
                cells = cuts.get("cells")
                pad_pt = float(cuts.get("pad_pt") or 0.0)
            elif isinstance(cuts, list):
                cols = [float(c) for c in cuts]
            nrows = len(rows) + 1; ncols = len(cols) + 1
            # 黑框路径: 实际检出了几个框就只切几段。最后一页只有 2 页 PPT 时,
            # 不能再按 nrows*ncols 补齐(那样会切出纯白页), 也不能让多余 seg 掉进等分回退。
            total_segs = len(cells) if cells else nrows * ncols
            if seg < 1 or seg > total_segs:
                doc.close()
                return None
            if cells and 1 <= seg <= len(cells):
                # 优先路径: cells 是 PPT 外框矩形, 直接以它为锚点 + 四边均匀 pad
                # 这样每个 cell 的切图都紧贴自己那张 PPT 的黑框, 白边宽度一致
                x0f, y0f, x1f, y1f = cells[seg - 1]
                w, h = page.rect.width, page.rect.height
                x0 = page.rect.x0 + w * x0f - pad_pt
                y0 = page.rect.y0 + h * y0f - pad_pt
                x1 = page.rect.x0 + w * x1f + pad_pt
                y1 = page.rect.y0 + h * y1f + pad_pt
                x0 = max(page.rect.x0, x0); x1 = min(page.rect.x1, x1)
                y0 = max(page.rect.y0, y0); y1 = min(page.rect.y1, y1)
                clip = fitz.Rect(x0, y0, x1, y1)
            elif cells:
                doc.close()
                return None
            else:
                row_idx = (seg - 1) // ncols
                col_idx = (seg - 1) % ncols
                xbounds = [0.0] + sorted(cols) + [1.0]
                ybounds = [0.0] + sorted(rows) + [1.0]
                w, h = page.rect.width, page.rect.height
                clip = fitz.Rect(
                    page.rect.x0 + w * xbounds[col_idx],
                    page.rect.y0 + h * ybounds[row_idx],
                    page.rect.x0 + w * xbounds[col_idx + 1],
                    page.rect.y0 + h * ybounds[row_idx + 1])
        pix = page.get_pixmap(dpi=120, clip=clip)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.save(p, "JPEG", quality=82)
        doc.close()
        return p
    except Exception:
        return None


# ---------- 一行多页(2-up/3-up)检测 ----------

def _looks_scanned(page) -> bool:
    """扫描/整页图片: 没有可投影的文字块, 且有一张近似整页的图片。
    这类页面几何上找不到白缝, 但仍可能是 2-up 扫描, 交给视觉模型判断。"""
    try:
        txt = [b for b in page.get_text("blocks") if len((b[4] or "").strip()) >= 2]
        if len(txt) >= 3:
            return False
        W, H = page.rect.width, page.rect.height
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                if r.width * r.height >= W * H * 0.8:
                    return True
    except Exception:
        pass
    return False


def _collect_body_rects(page):
    """提取整页上有意义的内容矩形(文字块/图片/非白底绘制), 过滤页眉页脚和全宽装饰。"""
    import fitz
    W, H = page.rect.width, page.rect.height
    rects = []
    try:
        for b in page.get_text("blocks"):
            rects.append(fitz.Rect(b[0], b[1], b[2], b[3]))
    except Exception:
        pass
    try:
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                rects.append(fitz.Rect(r))
    except Exception:
        pass
    try:
        for d in page.get_drawings():
            fill = d.get("fill")
            if fill and len(fill) >= 3 and all(c >= 0.92 for c in fill[:3]):
                continue   # 整页白底/白色装饰不算内容, 否则会把白缝填满
            r = fitz.Rect(d["rect"])
            if r.width >= W * 0.9 and r.height >= H * 0.9:
                continue
            rects.append(r)
    except Exception:
        pass
    body = []
    for r in rects:
        if r.is_empty or r.width < W * 0.02 or r.height < H * 0.02:
            continue
        if r.y1 <= H * 0.07 or r.y0 >= H * 0.93:   # 页眉/页脚不参与
            continue
        if r.width >= W * 0.9 and r.height < H * 0.08:  # 全宽装饰条
            continue
        body.append(r)
    return body, W, H


def _find_col_cuts(body, W, H, max_cuts=3) -> list:
    """找贯穿页面的纵向白缝 → cols (x 比例)。两侧都要有内容跨度 ≥ 25% 页高。"""
    if len(body) < 2:
        return []
    iv = sorted((max(0.0, r.x0), min(W, r.x1)) for r in body)
    merged = []
    for a, b in iv:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps, cur = [], 0.0
    for a, b in merged:
        if a - cur > 0:
            gaps.append((cur, a))
        cur = max(cur, b)
    if W - cur > 0:
        gaps.append((cur, W))
    cuts = []
    for g0, g1 in gaps:
        gw = g1 - g0
        if gw < W * 0.025 or gw > W * 0.50:
            continue
        if g0 < W * 0.15 or (W - g1) < W * 0.15:
            continue
        left = [r for r in body if r.x1 <= g0 + 1]
        right = [r for r in body if r.x0 >= g1 - 1]
        if not left or not right:
            continue
        if max(r.y1 for r in left) - min(r.y0 for r in left) < H * 0.25:
            continue
        if max(r.y1 for r in right) - min(r.y0 for r in right) < H * 0.25:
            continue
        cuts.append(round(((g0 + g1) / 2) / W, 3))
    cuts = sorted(set(cuts))[:max_cuts]
    out = []
    for c in cuts:
        if not out or c - out[-1] >= 0.2:
            out.append(c)
    return out


def _find_row_cuts(body, W, H, max_cuts=3) -> list:
    """找贯穿页面的横向白缝 → rows (y 比例)。两侧都要有内容跨度 ≥ 25% 页宽。"""
    if len(body) < 2:
        return []
    iv = sorted((max(0.0, r.y0), min(H, r.y1)) for r in body)
    merged = []
    for a, b in iv:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps, cur = [], 0.0
    for a, b in merged:
        if a - cur > 0:
            gaps.append((cur, a))
        cur = max(cur, b)
    if H - cur > 0:
        gaps.append((cur, H))
    cuts = []
    for g0, g1 in gaps:
        gh = g1 - g0
        if gh < H * 0.025 or gh > H * 0.50:
            continue
        if g0 < H * 0.15 or (H - g1) < H * 0.15:
            continue
        top = [r for r in body if r.y1 <= g0 + 1]
        bot = [r for r in body if r.y0 >= g1 - 1]
        if not top or not bot:
            continue
        if max(r.x1 for r in top) - min(r.x0 for r in top) < W * 0.25:
            continue
        if max(r.x1 for r in bot) - min(r.x0 for r in bot) < W * 0.25:
            continue
        cuts.append(round(((g0 + g1) / 2) / H, 3))
    cuts = sorted(set(cuts))[:max_cuts]
    out = []
    for c in cuts:
        if not out or c - out[-1] >= 0.2:
            out.append(c)
    return out


def _detect_frame_grids(page) -> Optional[dict]:
    """讲义 PDF 每页自带黑色描边 PPT 外框 — 直接用这些框作为切图锚点。
    返回 {rows:[], cols:[], cells:[[x0,y0,x1,y1]…归一化], pad_pt:float, origin:"frames"}。
    cells 按行主序 (左上→右上→左下→右下…) 排列。
    普通 PDF 无黑框 → 返回 None, 让 _layout_grid 回退到 body rects 白缝投影。"""
    import fitz
    W, H = page.rect.width, page.rect.height
    frames = []
    try:
        for d in page.get_drawings():
            kind = d.get("type") or ""
            r = fitz.Rect(d["rect"])
            if r.width >= W * 0.95 and r.height >= H * 0.95: continue
            if r.width < W * 0.15 or r.height < H * 0.15: continue
            if r.width > W * 0.85 or r.height > H * 0.85: continue
            fill = d.get("fill")
            if kind in ("s", "re") or (kind == "f" and d.get("color") is not None):
                if fill is None or all(c >= 0.90 for c in fill[:3]):
                    ar = r.width / max(r.height, 1e-6)
                    if 0.55 <= ar <= 1.80:
                        frames.append({"rect": r})
    except Exception: return None
    if len(frames) < 2 or len(frames) > 16: return None

    # 去重叠: 按面积降序先入, 大框(PPT 外框)先保留, 被大框包住的小装饰框自动丢弃
    frames.sort(key=lambda f: f["rect"].get_area(), reverse=True)
    deduped = []
    for f in frames:
        dup = False
        for u in deduped:
            inter = f["rect"] & u["rect"]
            if not inter.is_empty and inter.get_area() > 0.85 * min(f["rect"].get_area(), u["rect"].get_area()):
                dup = True; break
        if not dup: deduped.append(f)
    if len(deduped) < 2: return None

    # 按宽高聚类找尺寸一致的 PPT 外框组
    from collections import defaultdict
    # 用 5pt 粒度 coarse 宽高, 避免微小绘制偏差(如 288.5 vs 289.7pt)把同类框分到不同组
    def _szk(f):
        w = f["rect"].width; h = f["rect"].height
        return (round(w / 5) * 5, round(h / 5) * 5)
    groups = defaultdict(list)
    for f in deduped:
        groups[_szk(f)].append(f)
    best = None
    for key, grp in groups.items():
        if len(grp) < 2: continue
        w, h = key; ar = w / max(h, 1e-6)
        if 0.55 <= ar <= 1.80 and (best is None or len(grp) > len(best)):
            best = grp
    if not best or len(best) < 2: return None

    # 把这些外框按「行列网格」组织起来
    def _coarse(vals, tol=6.0):
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1]) < tol: continue
            out.append(v)
        return sorted(set(round(v, 0) for v in out))
    xs_sorted = _coarse([f["rect"].x0 for f in best])
    ys_sorted = _coarse([f["rect"].y0 for f in best])
    if len(xs_sorted) < 2 and len(ys_sorted) < 2: return None

    # 用外框位置推导 cols/rows 切分线 (取相邻组缝隙中心, 让非 frame 路径也可复用)
    cols, rows = [], []
    if len(xs_sorted) >= 2:
        for i in range(len(xs_sorted) - 1):
            prev_right = max(f["rect"].x1 for f in best if abs(f["rect"].x0 - xs_sorted[i]) < 8.0)
            next_left  = min(f["rect"].x0 for f in best if abs(f["rect"].x0 - xs_sorted[i + 1]) < 8.0)
            if next_left - prev_right >= W * 0.01:
                cols.append(round((prev_right + next_left) / 2 / W, 3))
    if len(ys_sorted) >= 2:
        for i in range(len(ys_sorted) - 1):
            prev_bot = max(f["rect"].y1 for f in best if abs(f["rect"].y0 - ys_sorted[i]) < 8.0)
            next_top = min(f["rect"].y0 for f in best if abs(f["rect"].y0 - ys_sorted[i + 1]) < 8.0)
            if next_top - prev_bot >= H * 0.01:
                rows.append(round((prev_bot + next_top) / 2 / H, 3))
    if not rows and not cols: return None

    # cells: 每个实际检出的 PPT 外框一段, 归一化坐标 [x0,y0,x1,y1], 行主序。
    # 讲义最后一页常常只印 1~3 张 PPT, 网格天然不完整 —— 以前这里 return None
    # 会退回等分整页, 于是多出来的两段就是整片白纸。现在只按真实框切。
    nrows = len(ys_sorted); ncols = len(xs_sorted)
    cells = []
    for f in sorted(best, key=lambda x: (round(x["rect"].y0 / max(H * 0.05, 1)), x["rect"].x0)):
        r = f["rect"]
        cells.append([round(r.x0/W, 4), round(r.y0/H, 4),
                      round(r.x1/W, 4), round(r.y1/H, 4)])
    if len(cells) < 2:
        return None

    # pad_pt: 很小的常量, 只是让 PPT 黑框周围露出一圈白纸 + 一点边界空白
    pad_pt = 3.0

    return {"rows": rows, "cols": cols, "cells": cells, "pad_pt": pad_pt,
            "segs": len(cells), "nrows": nrows, "ncols": ncols, "origin": "frames"}


def _layout_grid(page) -> Optional[dict]:
    """双向切分检测: 优先从 PPT 外框矩形定位 (讲义 PDF), 否则白缝投影 (普通 2-up/3-up)。
    返回 {rows:[], cols:[], pad_pt:float|None} 或 None。"""
    # 路径 1: 黑框矩形 (讲义 PDF) — 最准
    frame = _detect_frame_grids(page)
    if frame:
        rows, cols = frame["rows"], frame["cols"]
        if rows or cols:
            # 只对非单一方向的情况做误切防护 — 单一方向 (只有 rows 或只有 cols) 已由黑框检测保证
            if rows and cols:
                W, H = page.rect.width, page.rect.height
                total_area = W * H
                all_pts = sorted([0.0] + rows + [1.0])
                col_pts = sorted([0.0] + cols + [1.0])
                for i in range(len(all_pts) - 1):
                    for j in range(len(col_pts) - 1):
                        a = (col_pts[j + 1] - col_pts[j]) * W * \
                            (all_pts[i + 1] - all_pts[i]) * H
                        if a < total_area * 0.15:
                            return None
            return frame

    # 路径 2: body rects 白缝投影 (普通 PDF)
    body, W, H = _collect_body_rects(page)
    rows = _find_row_cuts(body, W, H)
    cols = _find_col_cuts(body, W, H)
    if not rows and not cols:
        return None
    # 误切防护 (面积 + 长宽比)
    all_pts = sorted([0.0] + rows + [1.0])
    col_pts = sorted([0.0] + cols + [1.0])
    cells = []
    for i in range(len(all_pts) - 1):
        for j in range(len(col_pts) - 1):
            x0, x1 = W * col_pts[j], W * col_pts[j + 1]
            y0, y1 = H * all_pts[i], H * all_pts[i + 1]
            cells.append((x0, y0, x1, y1))
    total_area = W * H
    for x0, y0, x1, y1 in cells:
        a = (x1 - x0) * (y1 - y0)
        if a < total_area * 0.15:
            return None
        bw, bh = x1 - x0, y1 - y0
        ar = bw / max(bh, 1e-6) if bh > 0 else 1.0
        ok = (0.6 <= ar <= 2.2)  # 更宽松, 因为是几何投影
        if not ok:
            return None
    return {"rows": rows, "cols": cols, "pad_pt": 0.0}


def _geom_cuts(page) -> list:
    """向后兼容: 返回 cols (旧 cuts 字段含义)。只有几何 cols、无 rows 的情况才走旧格式。"""
    g = _layout_grid(page)
    if g and not g.get("rows") and g.get("cols"):
        return g["cols"]
    return []


def _render_thumb_jpg(cid: str, fid: str, page) -> Optional[Path]:
    """给视觉模型看的缩略图(宽约 960px), 与预览缓存分开。"""
    p = _preview_dir(cid, fid) / f"thumb_{page.number + 1:03d}.jpg"
    if p.is_file():
        return p
    try:
        import fitz
        from PIL import Image
        zoom = 960 / max(1, page.rect.width)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.save(p, "JPEG", quality=80)
        return p
    except Exception:
        return None


def _vision_cuts(img_path: Optional[Path]) -> Optional[dict]:
    """只对几何候选页调用视觉模型, 返回 {rows:[], cols:[]} 或 None (失败/不确认)。"""
    if not img_path:
        return None
    cfg = load_config()
    key = cfg.get("sensenova_key") or os.environ.get("SENSENOVA_API_KEY", "")
    if not key:
        return None
    model = cfg.get("sensenova_model", "sensenova-6.8-flash-lite")
    try:
        b64 = base64.b64encode(img_path.read_bytes()).decode()
    except OSError:
        return None
    prompt = (
        "这是一页课件或文档截图。判断它是否把多页内容按【网格形式】排版在同一页。\n"
        "支持的形式: 一行两页(2-up)、一行三页、上下两页(纵向堆叠)、2x2四页、3x2等。\n"
        "只输出 JSON, 不要解释, 不要代码块标记:\n"
        "单页:   {\"multi\":false,\"rows\":[],\"cols\":[]}\n"
        "左右两页: {\"multi\":true,\"rows\":[],\"cols\":[0.5]}\n"
        "上下两页: {\"multi\":true,\"rows\":[0.5],\"cols\":[]}\n"
        "2x2四页:  {\"multi\":true,\"rows\":[0.5],\"cols\":[0.5]}\n"
        "rows 是 y 轴方向的横向白缝位置(0~1 的页高比例), cols 是 x 轴方向的纵向白缝位置(0~1 页宽比例)。"
    )
    body = {"model": model, "temperature": 0, "max_tokens": 1200,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
                {"type": "text", "text": prompt}]}]}
    try:
        r = httpx.post("https://token.sensenova.cn/v1/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {key}"}, timeout=90)
        r.raise_for_status()
        msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
        txt = msg.get("content") or msg.get("reasoning") or ""
        obj = _parse_json_loose(txt)
        if not obj.get("multi"):
            return None
        rows = sorted(round(float(r), 3) for r in (obj.get("rows") or []))
        cols = sorted(round(float(c), 3) for c in (obj.get("cols") or []))
        if not rows and not cols:
            return None
        for arr in (rows, cols):
            if any(not (0.12 < v < 0.88) for v in arr):
                return None
            if any(arr[i + 1] - arr[i] < 0.15 for i in range(len(arr) - 1)):
                return None
            if len(arr) > 3:
                return None
        return {"rows": rows, "cols": cols, "via": "vision"}
    except Exception:
        return None


# 切分算法版本: 改动算法后必须 +1。旧 layout.json / 旧切图缓存会自动作废,
# 否则「算法改了但页面没变化」——因为 page_NNN_S.jpg 一旦存在就直接复用。
LAYOUT_VERSION = 3


def detect_layout(cid: str, meta: dict, entry: dict, force: bool = False) -> dict:
    """两段式: 几何粗筛(宁可多报) + 只对候选页调视觉模型确认切点; 失败则不切。"""
    if entry.get("kind") not in PAGED_KINDS:
        return {"pages": {}, "detected": False}
    pdf = _entry_pdf(cid, meta, entry)
    if not pdf:
        return {"pages": {}, "detected": False, "error": "无法生成预览 PDF"}
    try:
        st = pdf.stat()
        key = f"v{LAYOUT_VERSION}-{st.st_mtime}-{st.st_size}"
    except OSError:
        return {"pages": {}, "detected": False}
    prev = _preview_dir(cid, entry["fid"])
    cache = prev / "layout.json"
    if not force:
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if old.get("key") == key and old.get("version") == LAYOUT_VERSION:
                return {"pages": old.get("pages") or {}, "detected": True, "cached": True}
        except Exception:
            pass
    pages, candidates = {}, 0
    try:
        import fitz
        doc = fitz.open(str(pdf))
        for i in range(len(doc)):
            page = doc[i]
            ggrid = _layout_grid(page)
            if not ggrid and not _looks_scanned(page):
                continue
            candidates += 1
            # 几何命中了: 已经通过严格的面积/长宽比校验, 直接采纳, 不浪费 vision API 配额
            # 只有几何全空(扫描文档)时才调 vision 兜底
            if ggrid:
                ggrid["via"] = "geom"
                pages[str(i + 1)] = ggrid
            else:
                vgrid = _vision_cuts(_render_thumb_jpg(cid, entry["fid"], page))
                if vgrid and (vgrid.get("rows") or vgrid.get("cols")):
                    pages[str(i + 1)] = vgrid
        doc.close()
    except Exception as e:
        return {"pages": {}, "detected": False, "error": str(e)[:120]}
    # 布局重新算过 -> 旧切图(尤其最后两页那种半页白纸)必须一起清掉,
    # 否则 _render_page_jpg 的「文件已存在就直接用」会让算法改动看起来没生效。
    try:
        for f in prev.glob("page_*.jpg"):
            f.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _atomic_write_json(cache, {"key": key, "version": LAYOUT_VERSION, "pages": pages})
    except Exception:
        pass
    return {"pages": pages, "detected": True, "candidates": candidates,
            "version": LAYOUT_VERSION}


def _read_text_file(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def build_context(meta: dict) -> str:
    """精简全局词表(30–50): 缩写/英文型号优先, 避免被长中文词挤掉。"""
    terms = [str(t).strip() for t in (meta.get("terms") or []) if str(t).strip()]
    seen, uniq = set(), []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    # 英文缩写/型号(如 MCU、I2C、STM32)最容易听错, 放在前面确保入选
    abbr = [t for t in uniq if re.fullmatch(r"[A-Za-z][A-Za-z0-9+\-./]{1,}", t)]
    picked = (abbr + [t for t in uniq if t not in abbr])[:48]
    ctx = f"《{meta['name']}》课堂录音。"
    if picked:
        ctx += "课程术语：" + "、".join(picked)
    return ctx


# ---------- qwen3-asr ----------

def qwen_asr(wav_path: Path, context: str) -> str:
    from dashscope import MultiModalConversation
    b64 = base64.b64encode(wav_path.read_bytes()).decode()
    msg = {"role": "user", "content": [{"audio": f"data:audio/wav;base64,{b64}"}]}
    # 官方支持的形态是 messages 里的 system 内容; parameters.asr_context 不会被当作术语偏置
    messages = [{"role": "system", "content": [{"text": context}]}, msg] if context else [msg]
    r = MultiModalConversation.call(model="qwen3-asr-flash", messages=messages)
    if r.status_code != 200:
        raise RuntimeError(f"ASR failed: {getattr(r, 'code', '')} {getattr(r, 'message', '')}")
    return r.output.choices[0].message.content[0]["text"]


# ---------- 截图 OCR (qwen-vl-ocr) ----------

OCR_PROMPT = ("识别图片中的全部文字。数学公式、符号、上下标等用 LaTeX 表示："
              "独立公式用 $$...$$，与文字混排的用 $...$；"
              "表格请用 Markdown 表格表示，不要用 \\begin{tabular}、\\begin{center} 等 LaTeX 排版环境。"
              "不要输出 ```latex 代码块标记，不要解释，只输出识别结果。")


def qwen_ocr(img_bytes: bytes, mime: str = "image/png") -> str:
    from dashscope import MultiModalConversation
    b64 = base64.b64encode(img_bytes).decode()
    msg = {"role": "user", "content": [
        {"image": f"data:{mime};base64,{b64}"},
        {"text": OCR_PROMPT}]}
    r = MultiModalConversation.call(model="qwen-vl-ocr", messages=[msg])
    if r.status_code != 200:
        raise RuntimeError(f"OCR failed: {getattr(r, 'code', '')} {getattr(r, 'message', '')}")
    content = r.output.choices[0].message.content or []
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("text"):
            parts.append(c["text"])
        elif isinstance(c.get("ocr_result"), dict) and c["ocr_result"].get("processed_text"):
            parts.append(c["ocr_result"]["processed_text"])
    text = "\n".join(parts).strip()
    text = re.sub(r"^```(?:latex|tex|markdown|md)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------- 后台流水线 ----------

AUDIO_WAV_LOCK = threading.Lock()


def _dur_of(f: Optional[Path]) -> float:
    if not f or not f.is_file():
        return 0.0
    p = run_quiet([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    try:
        return float(p.stdout.strip() or 0)
    except ValueError:
        return 0.0


def _decode_webm_chunks(sdir: Path, work: Path, sid: int) -> Optional[Path]:
    """把一个 session 的 48k webm 分块逐块解码(跳过 0 字节/损坏块)后拼成 16k 单声道 wav。
    ffmpeg 的重采样器自带抗混叠低通, 不会像手写线性插值那样把超声折叠成 6.6k 哨音。"""
    chunks = sorted(sdir.glob("rec_*.webm")) if sdir.is_dir() else []
    if not chunks:
        return None
    decoded = []
    for i, ck in enumerate(chunks):
        try:
            if ck.stat().st_size < 200:
                continue
        except OSError:
            continue
        out = work / f"dec_s{sid}_{i:04d}.wav"
        r = run_quiet([FFMPEG, "-v", "error", "-y", "-i", str(ck),
                            "-ac", "1", "-ar", "16000", str(out)], capture_output=True)
        if r.returncode == 0 and _dur_of(out) > 0.05:
            decoded.append(out)
        else:
            out.unlink(missing_ok=True)
    if not decoded:
        return None
    dst = work / f"webm_s{sid}.wav"
    if len(decoded) == 1:
        shutil.copyfile(decoded[0], dst)
    else:
        lst = work / f"list_webm_s{sid}.txt"
        lst.write_text("\n".join(f"file '{c.as_posix()}'" for c in decoded), encoding="utf-8")
        r = run_quiet([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-ac", "1", "-ar", "16000", str(dst)], capture_output=True)
        if r.returncode != 0 or not dst.is_file():
            return None
    return dst


def _session_wav(work: Path, sid: int) -> tuple[Optional[Path], str]:
    """一个 session 的音源选择: webm(干净)优先; 分块损坏/缺失/明显短时回退实时 PCM(旧数据可能带哨音)。"""
    with AUDIO_WAV_LOCK:
        webm = _decode_webm_chunks(work / "sessions" / f"s{sid}", work, sid)
        dw = _dur_of(webm)
        live = None
        dl = 0.0
        live_dir = work / "live" / f"s{sid}"
        live_wavs = sorted(live_dir.glob("live_*.wav")) if live_dir.is_dir() else []
        if live_wavs:
            lst = work / f"list_live_s{sid}.txt"
            lst.write_text("\n".join(f"file '{w.as_posix()}'" for w in live_wavs), encoding="utf-8")
            live = work / f"live_{sid}.wav"
            run_quiet([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-ac", "1", "-ar", "16000", str(live)],
                           capture_output=True)
            dl = _dur_of(live)
    # webm 覆盖度足够(相差 10% 以内)就用它; 分块缺失过多则回退实时 PCM
    if webm and dw >= max(1.0, dl * 0.9):
        return webm, "webm"
    if live and dl > 1:
        return live, "live"
    if webm and dw > 1:
        return webm, "webm"
    return None, ""


def run_transcribe(cid: str):
    """下课自动跑: 拼接所有上课session的录音 -> 转写 -> 字幕稿 (不做AI整理)"""
    d = cdir(cid)
    meta = load_meta(cid)
    work = d / ".work"
    try:
        meta["status"] = "transcribing"
        save_meta(cid, meta)

        # 每个 session: 优先 48k webm(ffmpeg 正规重采样, 无哨音), 回退实时 PCM
        sessions = meta.get("sessions") or [{"sid": 0}]
        session_wavs = []
        for s in sessions:
            best, _src = _session_wav(work, s["sid"])
            if best and best.is_file():
                session_wavs.append(best)

        if not session_wavs:
            raise RuntimeError("没有可用的录音数据")
        if len(session_wavs) == 1:
            shutil.copyfile(session_wavs[0], work / "full.wav")
        else:
            lst = work / "list_all.txt"
            lst.write_text("\n".join(f"file '{w.as_posix()}'" for w in session_wavs), encoding="utf-8")
            run_quiet([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-ac", "1", "-ar", "16000", str(work / "full.wav")],
                           capture_output=True)
        full = work / "full.wav"

        p = run_quiet([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(full)], capture_output=True, text=True)
        meta["duration"] = int(float(p.stdout.strip() or 0))
        save_meta(cid, meta)

        # 分段转写 -> 字幕稿
        for old in work.glob("seg_*.wav"):
            old.unlink()
        run_quiet([FFMPEG, "-v", "error", "-y", "-i", str(full), "-f", "segment",
                        "-segment_time", str(SEG_SECONDS), "-ac", "1", "-ar", "16000",
                        str(work / "seg_%03d.wav")], check=True, capture_output=True)
        segs = sorted(work.glob("seg_*.wav"))
        context = build_context(meta)
        terms_on = load_config().get("asr_terms", True)
        parts = []
        for i, seg in enumerate(segs):
            text = qwen_asr(seg, context).strip()
            # 转写后、AI 复核前的确定性修正; 缩写只替换术语白名单命中的片段
            text = asr_terms.apply(text, terms=meta.get("terms") or [], enabled=terms_on)
            parts.append(f"[{fmt_ts(i * SEG_SECONDS)}] {text}")
            meta["progress"] = f"{i + 1}/{len(segs)}"
            save_meta(cid, meta)
        meta["transcript"] = "\n".join(parts)
        meta["status"] = "recorded"   # 转写完成, 等待手动整理
        save_meta(cid, meta)

        # 整堂音频归档到「录音原稿」, 与各次回放放在一起
        raw_dir = d / "录音原稿"
        raw_dir.mkdir(parents=True, exist_ok=True)
        run_quiet([FFMPEG, "-v", "error", "-y", "-i", str(full), "-b:a", "48k",
                        str(raw_dir / "课堂全程.mp3")], check=True, capture_output=True)
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = f"{type(e).__name__}: {e}"
        save_meta(cid, meta)


def run_organize(cid: str):
    """手动触发: 根据转写稿 + PPT 生成按页讲稿和思维导图"""
    d = cdir(cid)
    meta = load_meta(cid)
    try:
        transcript = meta.get("transcript") or meta.get("live_transcript") or ""
        if not transcript:
            raise RuntimeError("没有转写内容")
        meta["status"] = "organizing"
        save_meta(cid, meta)

        slides_txt = meta.get("slides_text", "") or "（本课程未上传PPT）"
        if len(slides_txt) > 12000:
            slides_txt = slides_txt[:12000] + "\n...(截断)"
        # 课堂开关: 开启后才把本课程的课程文件纳入讲稿/思维导图参考资料
        ref_txt = ""
        if meta.get("use_course_files") and meta.get("group_id"):
            ref_txt = collect_course_files_text(meta["group_id"], limit=8000)
        n_pages = len(meta.get("slides") or [])
        sys1 = "你是课程笔记整理助手，输出为简体中文Markdown。"
        p1 = f"""下面是一节课的PPT内容和课堂录音转写稿。请对照PPT修正转写稿中的听写错误（尤其是专业术语），
把老师的讲解对应到PPT的每一页，输出Markdown，结构严格如下：

# 讲课复现：{meta['name']}
## 本课要点（5-10条，简洁）
## 分页讲稿
{f"（PPT共{n_pages}页）" if n_pages else ""}
每一页一个小节，标题格式必须是 `### 第N页：<页面主题>`（N为PPT页码）。
小节内写：老师讲这一页时说了什么（含例子、推导、时间点如 [03:12]），
强调的内容标注"老师强调"；录音完全没提到的页写一句话概括该页内容即可。
按老师的实际讲授顺序排列页码；若老师跳页或回讲，如实反映。
不要输出「疑问与待复习」之类的附加章节，到最后一页讲稿结束即可。

--- PPT 内容 ---
{slides_txt}
{("--- 课程参考文件（本课程文件库，可用来订正术语、补充背景，不要按页对应） ---" + chr(10) + ref_txt) if ref_txt else ""}

--- 转写稿（[mm:ss]为时间点，可能有错别字） ---
{transcript}
"""
        meta["notes_md"] = llm_chat(p1, sys1)
        save_meta(cid, meta)

        p2 = f"""根据下面的课程笔记，输出一张思维导图，格式为Markdown嵌套无序列表（用 - 和两空格缩进表示层级），
根节点是课程名，层级3-4层，只输出列表本身：
{meta['notes_md'][:6000]}"""
        meta["mindmap_md"] = llm_chat(p2, max_tokens=2000).strip()
        meta["status"] = "done"
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        save_meta(cid, meta)
    except Exception as e:
        meta["status"] = "recorded"
        meta["error"] = f"{type(e).__name__}: {e}"
        save_meta(cid, meta)


# ---------- 录音原稿 AI 复审 ----------
# ASR 常把希腊字母/数学符号写成中文读音(「sin西塔」「阿尔法系数」「派比二」)。
# 这里手动触发一次「最小订正」复审: live_transcript / transcript 原稿永不改动,
# 结果单独存 meta.reviewed_transcript, 并留档到 课程库/<课程>/<课堂>/录音原稿/AI复审稿.md。
# 实测: SenseNova 6.8 是推理型模型, 必须传 thinking={"type":"disabled"},
# 否则 content 为空、finish_reason=length, 59 秒也拿不到校对结果。
REVIEW_CHUNK_CHARS = 2000
REVIEW_MAX_TOKENS = 5000
REVIEW_SYS = ("你是课堂录音转写稿的校对助手。只做最小限度订正，输出纯文本，"
              "不要输出任何解释、标题、前后缀或 Markdown 代码块。")


def _review_prompt(text: str, terms: str) -> str:
    return f"""下面是一段课堂录音转写稿，由语音识别生成，其中部分希腊字母、数学符号和专业术语被识别成了中文读音或错字。

请按以下规则订正：
1. 把写成中文读音的希腊字母和数学符号还原成对应符号，例如：
   「西塔」→θ、「阿尔法」→α、「贝塔」→β、「伽马」→γ、「德尔塔」→δ（表示增量时可用Δ）、
   「欧米伽/欧米加」→ω或Ω（按语境）、「派」→π、「西格玛」→σ、「拉姆达」→λ、
   「缪」→μ、「艾普西隆」→ε、「伊塔」→η、「柔」→ρ、「陶」→τ、「普赛」→ψ。
   组合一起处理：「sin西塔」→「sinθ」，「除以西塔」→「除以θ」，「阿尔法系数」→「α系数」。
2. 顺带订正明显的同音术语错字（可参考课程术语表）。
3. 严格保留每一行的 [mm:ss] 时间戳、原有换行与句子顺序；不要增删句子、不要润色、
   不要改写语气、不要总结、不要补充原文没有的内容。拿不准的地方原样保留。
4. 只输出订正后的全文。

--- 课程术语（可参考） ---
{terms}

--- 转写稿 ---
{text}"""


def _review_chunks(text: str, limit: int = REVIEW_CHUNK_CHARS) -> list:
    """按整行分块, 绝不切断 [mm:ss] 行。"""
    out, buf, size = [], [], 0
    for ln in text.split("\n"):
        if buf and size + len(ln) + 1 > limit:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    return [c for c in out if c.strip()]


def _review_acceptable(src: str, out: str) -> bool:
    """输出必须保持逐行结构与时间戳, 否则宁可整块回退原文, 绝不破坏原稿。"""
    if not out.strip():
        return False
    if src.count("\n") != out.count("\n"):
        return False
    if re.findall(r"\[\d{1,2}:\d{2}\]", src) != re.findall(r"\[\d{1,2}:\d{2}\]", out):
        return False
    if not (0.7 * len(src) <= len(out) <= 1.3 * len(src) + 40):
        return False
    if "```" in out or re.match(r"^(订正后|以下是|说明|好的|我已)", out.strip()):
        return False
    return True


def run_transcript_review(cid: str):
    initial = load_meta(cid)
    src = str(initial.get("live_transcript") or initial.get("transcript") or "")
    try:
        chunks = _review_chunks(src)
        terms = "、".join(str(t) for t in (initial.get("terms") or [])[:40]) or "（无）"
        parts, fallback = [], 0
        for i, ch in enumerate(chunks):
            m = load_meta(cid)
            m["review_progress"] = f"{i + 1}/{len(chunks)}"
            save_meta(cid, m)
            keep = ch
            try:
                out = llm_chat(_review_prompt(ch, terms), REVIEW_SYS,
                               max_tokens=REVIEW_MAX_TOKENS, plain=True).strip()
                out = re.sub(r"^```(?:text|markdown|md)?\s*|\s*```$", "", out).strip()
                if _review_acceptable(ch, out):
                    keep = out
                else:
                    fallback += 1
            except Exception as e:
                print(f"[review] {cid} chunk {i + 1} 失败，回退原文: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                fallback += 1
            parts.append(keep)
            time.sleep(0.8)   # 轻限速, 避免连续分块触发 429
        result = "\n".join(parts)
        m = load_meta(cid)
        m["reviewed_transcript"] = result
        m["review_status"] = "done"
        m["review_progress"] = f"{len(chunks)}/{len(chunks)}"
        m["reviewed_at"] = _now_iso()
        m["review_model"] = "SenseNova (thinking disabled)"
        m["review_fallback"] = fallback
        m.pop("review_error", None)
        save_meta(cid, m)       # 同时把 AI复审稿.md 同步到课程目录
        try:
            (cdir(cid) / "transcript_review.md").write_text(result, encoding="utf-8")
        except OSError:
            pass
    except Exception as e:
        m = load_meta(cid)
        m["review_status"] = "error"
        m["review_error"] = f"{type(e).__name__}: {e}"[:300]
        save_meta(cid, m)


# ---------- API ----------

@app.on_event("startup")
def _startup_groups():
    # 先把数据搬到项目根(课程库 + 隐藏技术文件), 再把课堂里的散落文件归位
    rep = migrate_layout()
    ensure_groups()
    rebuild_class_index()
    if rep["promoted"] or rep["tidied"] or rep["errors"]:
        print(f"[layout] promoted={len(rep['promoted'])} tidied={rep['tidied']} "
              f"errors={rep['errors'][:3]}", flush=True)
    # 课程目录在后台补齐可读文本, 不阻塞页面首次加载
    threading.Thread(target=_archive_sync_all, name="archive-sync", daemon=True).start()
    # 上次没合成完的回放音频(合成时被重启打断)在这里自愈
    threading.Thread(target=_resume_missing_audio, name="audio-resume", daemon=True).start()
    # 旧版本写入的绝对时间戳在这里增量换算成本次课内的相对时间
    threading.Thread(target=_convert_rel_timestamps, name="ts-conv", daemon=True).start()


# ---------- 界面状态备份 ----------
# 桌面壳端口不固定(8321 被占用时随机换端口), 而 localStorage 按 origin 隔离,
# 端口一变浏览器里的偏好/翻阅位置就全丢了。这里落盘到 .ui_state.json 做双备份。
UI_STATE_FILE = DATA / ".ui_state.json"
UI_STATE_LOCK = threading.Lock()
UI_SECTIONS = {"view_modes", "preview_files", "preview_pos"}


def _ui_state_read() -> dict:
    try:
        v = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/api/ui/state")
def ui_state_get():
    with UI_STATE_LOCK:
        return _ui_state_read()


@app.patch("/api/ui/state")
async def ui_state_patch(request: Request):
    p = await request.json()
    sec, key = p.get("section"), str(p.get("key", ""))
    if sec not in UI_SECTIONS or not key:
        raise HTTPException(400, "section/key 不合法")
    with UI_STATE_LOCK:
        st = _ui_state_read()
        st.setdefault(sec, {})[key] = p.get("value")
        _atomic_write_json(UI_STATE_FILE, st)
    return {"ok": True}


@app.get("/api/archive/tree")
def archive_tree():
    return _archive_tree()


@app.post("/api/archive/sync")
def archive_sync():
    r = _archive_sync_all()
    return {**r, "tree": _archive_tree()}


@app.get("/api/groups")
def list_groups():
    groups = sorted(load_groups(), key=lambda g: (g.get("order", 0), g.get("created", "")))
    counts = {}
    if DATA.is_dir():
        for _cid, d in iter_class_dirs():
            mf = d / "meta.json"
            if not mf.is_file():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            gid = m.get("group_id")
            if gid:
                counts[gid] = counts.get(gid, 0) + 1
    return [{**g, "class_count": counts.get(g["id"], 0),
             "file_count": course_files_count(g["id"])} for g in groups]


@app.post("/api/groups")
def create_group(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "课程名称不能为空")
    with GROUPS_LOCK:
        raw = _read_groups_raw()
        groups = [g for g in raw.get("groups", [])
                  if isinstance(g, dict) and g.get("id")] if isinstance(raw.get("groups"), list) else []
        deleted_default = bool(raw.get("default_deleted"))
        if not deleted_default and DEFAULT_GROUP not in {g["id"] for g in groups}:
            groups.insert(0, {"id": DEFAULT_GROUP, "name": DEFAULT_GROUP_NAME,
                              "created": _now_iso(), "order": 0})
        g = {"id": "g_" + uuid.uuid4().hex[:10], "name": name,
             "created": _now_iso(), "order": _next_group_order(groups)}
        groups.append(g)
        out = {"version": 1, "groups": groups}
        if deleted_default:
            out["default_deleted"] = True
        _atomic_write_json(GROUPS_FILE, out)
    return {**g, "class_count": 0}


@app.patch("/api/groups/{gid}")
def rename_group(gid: str, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "课程名称不能为空")
    with GROUPS_LOCK:
        raw = _read_groups_raw()
        groups = raw.get("groups") if isinstance(raw.get("groups"), list) else []
        hit = None
        for g in groups:
            if isinstance(g, dict) and g.get("id") == gid:
                hit = g
                break
        if not hit:
            raise HTTPException(404, "course group not found")
        old_name = hit.get("name") or ""
        hit["name"] = name
        out = {"version": 1, "groups": groups}
        if raw.get("default_deleted"):
            out["default_deleted"] = True
        _atomic_write_json(GROUPS_FILE, out)
    # 课程改名: 该课程下所有课堂的真实目录一起搬到新名字的文件夹
    for c in _group_classes(gid):
        try:
            relocate_class(c["id"])
        except Exception:
            pass
    # 旧名字的课程目录搬空后删掉, 不留空壳
    if old_name and old_name != name:
        old_dir = ARCHIVE_ROOT / _archive_safe(old_name, "")
        try:
            if old_dir.is_dir() and not any(old_dir.iterdir()):
                old_dir.rmdir()
        except OSError:
            pass
    return hit


def _group_classes(gid: str) -> list:
    out = []
    if not DATA.is_dir():
        return out
    for _cid, d in sorted(iter_class_dirs(), key=lambda t: t[0], reverse=True):
        mf = d / "meta.json"
        if not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("group_id") == gid:
            out.append(_meta_summary(m))
    return out


@app.get("/api/groups/{gid}/classes")
def list_group_classes(gid: str):
    if gid not in _group_ids():
        raise HTTPException(404, "course group not found")
    return _group_classes(gid)


@app.delete("/api/groups/{gid}")
def delete_group(gid: str, mode: str = "cascade", files: str = "keep"):
    """删除课程。mode=cascade: 课堂目录移入废纸篓; mode=detach: 课堂移到主界面(不归属)。
    files=delete: 课程文件一起移入废纸篓; files=keep: 课程文件移到独立位置保留(data/.course_files/)。
    删除前会把 groups.json 和课堂内容一并备份到 data/.trash/ 下。
    默认课程 g_default 同样可删(以前被硬编码保护, 用户改名为「测试」后一直删不掉)。"""
    if mode not in ("cascade", "detach"):
        raise HTTPException(400, "mode 仅支持 cascade / detach")
    if files not in ("keep", "delete"):
        raise HTTPException(400, "files 仅支持 keep / delete")
    files_info = None
    with GROUPS_LOCK:
        raw = _read_groups_raw()
        groups = raw.get("groups") if isinstance(raw.get("groups"), list) else []
        if not any(isinstance(g, dict) and g.get("id") == gid for g in groups):
            raise HTTPException(404, "course group not found")
        classes = _group_classes(gid)
        backup = _trash_dir(f"group-{gid}")
        _atomic_write_json(backup / "groups.json", {"version": 1, "groups": groups})
        moved = 0
        for c in classes:
            try:
                d = cdir(c["id"])
            except HTTPException:
                continue
            if mode == "cascade":
                try:
                    shutil.move(str(d), str(backup / c["id"]))
                    with CLASS_INDEX_LOCK:
                        CLASS_INDEX.pop(c["id"], None)
                except OSError as e:
                    raise HTTPException(500, f"课堂 {c['id']} 移入废纸篓失败（文件可能被占用）：{e}")
            else:
                m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                m["group_id"] = None
                _atomic_write_json(d / "meta.json", m)
                # 目录要跟着搬到「未归属课程」, 否则文件夹与归属不一致
                relocate_class(c["id"], m.get("name"), None)
            moved += 1
        rest = [g for g in groups if not (isinstance(g, dict) and g.get("id") == gid)]
        out = {"version": 1, "groups": rest}
        if gid == DEFAULT_GROUP or raw.get("default_deleted"):
            # 记住「默认课程已被用户删除」, 启动时不自动重建
            out["default_deleted"] = True
        _atomic_write_json(GROUPS_FILE, out)
        # 课程文件库: 有文件时必须显式处理, 不静默丢弃
        cdir_src = _course_dir(gid)
        n_files = course_files_count(gid)
        if n_files:
            if files == "delete":
                dst = backup / "course_files"
                dst.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cdir_src), str(dst / gid))
                files_info = {"count": n_files, "action": "delete",
                              "location": str((dst / gid).relative_to(DATA))}
            else:
                keep_root = DATA / ".courses" / "_已删除"
                keep_root.mkdir(parents=True, exist_ok=True)
                dst = keep_root / f"{gid}-{datetime.now():%Y%m%d-%H%M%S}"
                shutil.move(str(cdir_src), str(dst))
                files_info = {"count": n_files, "action": "keep",
                              "location": str(dst.relative_to(DATA))}
        elif cdir_src.is_dir():
            shutil.rmtree(cdir_src, ignore_errors=True)
    return {"ok": True, "mode": mode, "affected": moved,
            "backup": str(backup.relative_to(DATA)), "course_files": files_info}


@app.post("/api/courses")
async def create_course(request: Request):
    # 用原始表单解析, 区分"未传 group_id"(旧客户端, 归入默认课程) 与"显式传空值"(放在主界面)
    form = await request.form()
    name = str(form.get("name") or "").strip() or "未命名课堂"
    raw_gid = form.get("group_id")
    if raw_gid is None:
        gid = DEFAULT_GROUP
    else:
        gid = str(raw_gid).strip()
        if gid and gid not in _group_ids():
            raise HTTPException(400, "course group not found")
        gid = None if not gid else gid
    ensure_groups(meta_only_migration=False)
    cid = uuid.uuid4().hex[:12]
    d = class_target(cid, name, gid)
    if d.exists():
        d = d.parent / f"{d.name}-{cid[:6]}"
    (d / ".work" / "chunks").mkdir(parents=True, exist_ok=True)
    register_class(cid, d)
    meta = {"id": cid, "name": name,
            "group_id": gid,
            "created": _now_iso(),
            "status": "ready", "has_ppt": False, "terms": [],
            "slides": [], "slides_text": "", "transcript": "",
            "notes_md": "", "mindmap_md": "", "duration": 0, "progress": "",
            "live_transcript": "", "live_notes": ""}
    save_meta(cid, meta)
    return meta


@app.get("/api/courses")
def list_courses(group_id: Optional[str] = None):
    out = []
    if DATA.is_dir():
        for _cid, d in sorted(iter_class_dirs(), key=lambda t: t[0], reverse=True):
            mf = d / "meta.json"
            if not mf.is_file():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if group_id is not None:
                if group_id == "none":
                    if m.get("group_id"):
                        continue
                elif m.get("group_id") != group_id:
                    continue
            out.append(_meta_summary(m))
    return out


def audio_dir(cid: str) -> Path:
    d = cdir(cid) / "录音原稿"
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_path(cid: str, sid: int) -> Path:
    return audio_dir(cid) / f"audio_s{sid}.mp3"


def _audio_sids(d: Path) -> list[int]:
    """实际存在的回放音频(按 sid 排序); 删除录音后对应文件不存在, 不再计数。"""
    out = []
    adir = d / "录音原稿"
    if not adir.is_dir():
        return out
    for f in adir.glob("audio_s*.mp3"):
        m = re.fullmatch(r"audio_s(\d+)\.mp3", f.name)
        if m and f.stat().st_size > 0:
            out.append(int(m.group(1)))
    return sorted(out)


@app.get("/api/courses/{cid}")
def get_course(cid: str):
    meta = load_meta(cid)
    if ensure_file_entries(cid, meta):
        save_meta(cid, meta)
    meta["audio_count"] = len(_audio_sids(cdir(cid)))
    return meta


@app.post("/api/courses/{cid}/move")
def move_course(cid: str, group_id: str = Form("")):
    """把课堂移动到其他课程; group_id 为空表示移到主界面(不归属课程)。"""
    gid = group_id.strip()
    if gid and gid not in _group_ids():
        raise HTTPException(404, "course group not found")
    meta = load_meta(cid)
    relocate_class(cid, meta.get("name"), gid or None)
    meta["group_id"] = gid or None
    save_meta(cid, meta)
    return {"ok": True, "group_id": meta["group_id"]}


@app.patch("/api/courses/{cid}/use_course_files")
async def set_use_course_files(cid: str, request: Request):
    """课堂开关: 是否把本课程的课程文件纳入热词抽取与讲稿/思维导图参考(默认关闭)。"""
    payload = await request.json()
    on = bool(payload.get("on"))
    meta = load_meta(cid)
    meta["use_course_files"] = on
    save_meta(cid, meta)
    return {"ok": True, "use_course_files": on}


@app.patch("/api/courses/{cid}/name")
def rename_course(cid: str, name: str = Form(...)):
    """课堂改名: 只改这个课堂的显示名(meta.name), 不影响所属课程名。"""
    name = name.strip()
    if not name:
        raise HTTPException(400, "课堂名称不能为空")
    if len(name) > 80:
        raise HTTPException(400, "课堂名称过长（最多 80 字）")
    meta = load_meta(cid)
    relocate_class(cid, name, meta.get("group_id"))
    meta["name"] = name
    save_meta(cid, meta)
    return {"ok": True, "name": name}


@app.patch("/api/courses/{cid}/terms")
async def update_terms(cid: str, request: Request):
    """编辑热词: 去重(忽略大小写)、去空、单条≤40字、最多60条; 下一段实时转写立即生效。"""
    payload = await request.json()
    raw = payload.get("terms")
    if not isinstance(raw, list):
        raise HTTPException(400, "terms 必须是字符串数组")
    out, seen = [], set()
    for t in raw:
        if not isinstance(t, str):
            raise HTTPException(400, "terms 必须是字符串数组")
        s = re.sub(r"\s+", " ", t).strip()
        if not s:
            continue
        if len(s) > 40:
            raise HTTPException(400, f"术语过长（最多 40 字）：{s[:20]}…")
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    out = out[:60]
    meta = load_meta(cid)
    meta["terms"] = out
    # 手动填写即视为已解决抽取失败, 清掉失败提示
    meta["terms_error"] = None
    save_meta(cid, meta)
    return {"ok": True, "terms": out}


@app.post("/api/courses/{cid}/terms/extract")
def reextract_terms(cid: str):
    """从课堂文件库/笔记里的全部可读文本重新抽取(不再依赖 PPT); 失败时不覆盖已有 terms/outline。"""
    meta = load_meta(cid)
    t0 = time.time()
    col = collect_course_text(cid, meta)
    if not col["text"].strip():
        msg = _text_missing_msg(col)
        meta["terms_error"] = msg
        save_meta(cid, meta)
        raise HTTPException(400, msg)
    terms, outline, err = extract_terms_outline(col["text"], meta.get("name") or "")
    if err and not terms:
        meta["terms_error"] = err
        save_meta(cid, meta)
        raise HTTPException(502, err)
    apply_terms_result(meta, terms, outline, err)
    save_meta(cid, meta)
    return {"ok": True, "terms": meta.get("terms") or [],
            "outline": meta.get("outline") or [],
            "terms_error": meta.get("terms_error"),
            "chars": col["chars"], "sources": col["sources"], "skipped": col["skipped"],
            "elapsed": round(time.time() - t0, 1)}


# ---------- 热词/大纲后台抽取 ----------
# 之前是"上传 PPT 时同步抽取", 上传接口要等抽取跑完(最长 300 秒), 页面被卡住,
# 中途切走就像被打断。现在改成后台线程: 上传立刻返回, 切页面/进上课页都不影响。
TERMS_JOBS = {}                 # cid -> {"state": "running|done|error", "at": ts, "error": str}
TERMS_JOBS_LOCK = threading.Lock()


def _set_terms_job(cid: str, state: str, error: str = ""):
    with TERMS_JOBS_LOCK:
        TERMS_JOBS[cid] = {"state": state, "at": time.time(), "error": error}
    try:
        meta = load_meta(cid)
        meta["terms_job"] = {"state": state, "at": int(time.time()), "error": error}
        save_meta(cid, meta)
    except Exception:
        pass


def _run_terms_job(cid: str, course_name: str = ""):
    """后台抽取热词/大纲; 失败只记录原因, 不影响课堂其它功能。"""
    try:
        meta = load_meta(cid)
        col = collect_course_text(cid, meta)
        if col["text"].strip():
            terms, outline, err = extract_terms_outline(col["text"],
                                                        course_name or meta.get("name") or "")
        else:
            terms, outline, err = [], [], _text_missing_msg(col)
        apply_terms_result(meta, terms, outline, err)
        save_meta(cid, meta)
        _set_terms_job(cid, "done" if terms else "error", err or "")
        print(f"[terms] {cid} done: {len(terms)} terms", flush=True)
    except Exception as e:
        _set_terms_job(cid, "error", f"{type(e).__name__}: {e}")
        print(f"[terms] {cid} failed: {e}", file=sys.stderr, flush=True)


def _start_terms_job(cid: str, course_name: str = ""):
    """启动后台抽取; 已有任务在跑就跳过(不重复烧 token)。"""
    with TERMS_JOBS_LOCK:
        cur = TERMS_JOBS.get(cid)
        if cur and cur.get("state") == "running":
            return
    threading.Thread(target=_run_terms_job, args=(cid, course_name),
                     name="terms-job", daemon=True).start()


# ---------- 课堂问答记录(按课堂落盘, 不写进 meta) ----------
CHAT_MAX_MSGS = 60
CHAT_MAX_CHARS = 4000


def _chat_file(cid: str) -> Path:
    return cdir(cid) / "AI问答" / "当前会话.json"


@app.get("/api/courses/{cid}/chat")
def get_chat_history(cid: str):
    p = _chat_file(cid)
    if not p.is_file():
        return {"messages": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"messages": []}
    msgs = data.get("messages") if isinstance(data, dict) else data
    return {"messages": msgs if isinstance(msgs, list) else []}


@app.put("/api/courses/{cid}/chat")
async def put_chat_history(cid: str, request: Request):
    payload = await request.json()
    raw = payload.get("messages")
    if not isinstance(raw, list):
        raise HTTPException(400, "messages 必须是数组")
    out = []
    for m in raw[-CHAT_MAX_MSGS:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        item = {"role": m["role"], "content": str(m.get("content") or "")[:CHAT_MAX_CHARS]}
        if m.get("notice"):
            item["notice"] = str(m["notice"])[:400]
        src = m.get("sources")
        if isinstance(src, list):
            item["sources"] = [{"title": str(s.get("title") or "")[:160],
                                "url": str(s.get("url") or "")[:300]}
                               for s in src[:5] if isinstance(s, dict)]
        out.append(item)
    p = _chat_file(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, {"version": 1, "messages": out})
    # 每次对话都同步一份到 课程库/<课程>/<课堂>/AI问答/, 保证一定留档
    try:
        _archive_sync(cid, ["AI问答"])
    except Exception:
        pass
    return {"ok": True, "count": len(out)}


@app.delete("/api/courses/{cid}/chat")
def clear_chat_history(cid: str):
    """清空当前会话, 但先把原会话归档(界面清空 ≠ 数据删除)。"""
    p = _chat_file(cid)
    archive = None
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            msgs = data.get("messages") if isinstance(data, dict) else data
        except (OSError, json.JSONDecodeError):
            msgs = []
        if isinstance(msgs, list) and msgs:
            adir = cdir(cid) / "AI问答" / "历史"
            adir.mkdir(parents=True, exist_ok=True)
            archive = adir / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.json"
            _atomic_write_json(archive, {"version": 1, "messages": msgs,
                                         "cleared_at": _now_iso()})
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(p, {"version": 1, "messages": []})
    try:
        _archive_sync(cid, ["AI问答"])
    except Exception:
        pass
    return {"ok": True, "archived": str(archive.relative_to(DATA)).replace("\\", "/") if archive else None}


@app.delete("/api/courses/{cid}")
def delete_course(cid: str):
    d = cdir(cid)
    backup = _trash_dir(f"class-{cid}")
    try:
        shutil.move(str(d), str(backup / cid))
        with CLASS_INDEX_LOCK:
            CLASS_INDEX.pop(cid, None)
    except OSError as e:
        raise HTTPException(500, f"课堂目录移入废纸篓失败（Windows 文件被占用时会发生，关掉正在播放的音频/预览后重试）：{e}")
    return {"ok": True, "backup": str(backup.relative_to(DATA))}


# ---------- 卡片封面 / 页面背景 ----------

@app.post("/api/cover")
def upload_cover(file: UploadFile = File(...)):
    """封面图: 校验格式与大小, 居中裁成 16:9 并压到 1280x720 以内再落盘。"""
    from PIL import Image
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "图片为空")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(400, "图片过大（>12MB）")
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise HTTPException(400, "不是有效的图片文件")
    im = im.convert("RGB")
    w, h = im.size
    target = 16 / 9
    if w / h > target:
        nw = int(h * target)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im.thumbnail((1280, 720), Image.LANCZOS)
    COVERS.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    im.save(COVERS / name, "JPEG", quality=82, optimize=True)
    return {"url": f"/api/cover/{name}"}


@app.post("/api/pagebg")
def upload_page_bg(file: UploadFile = File(...)):
    """页面底图: 校验格式与大小, 等比压到 1920x1080 以内(不裁剪, 前端用 cover 铺放)。"""
    from PIL import Image
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "图片为空")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(400, "图片过大（>12MB）")
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise HTTPException(400, "不是有效的图片文件")
    im = im.convert("RGB")
    im.thumbnail((1920, 1080), Image.LANCZOS)
    COVERS.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + ".jpg"
    im.save(COVERS / name, "JPEG", quality=82, optimize=True)
    return {"url": f"/api/cover/{name}"}


@app.get("/api/cover/{name}")
def get_cover(name: str):
    if not re.match(r"^[0-9a-f]{32}\.jpg$", name):
        raise HTTPException(404, "cover not found")
    f = COVERS / name
    if not f.is_file():
        raise HTTPException(404, "cover not found")
    return FileResponse(f, media_type="image/jpeg")


@app.patch("/api/courses/{cid}/cover")
async def set_course_cover(cid: str, request: Request):
    cv = _valid_cover((await request.json()).get("cover"))
    meta = load_meta(cid)
    if cv is None:
        meta.pop("cover", None)
    else:
        meta["cover"] = cv
    save_meta(cid, meta)
    return {"ok": True, "cover": cv}


@app.patch("/api/groups/{gid}/cover")
async def set_group_cover(gid: str, request: Request):
    cv = _valid_cover((await request.json()).get("cover"))
    with GROUPS_LOCK:
        raw = _read_groups_raw()
        groups = raw.get("groups") if isinstance(raw.get("groups"), list) else []
        hit = next((g for g in groups if isinstance(g, dict) and g.get("id") == gid), None)
        if not hit:
            raise HTTPException(404, "course group not found")
        if cv is None:
            hit.pop("cover", None)
        else:
            hit["cover"] = cv
        out = {"version": 1, "groups": groups}
        if raw.get("default_deleted"):
            out["default_deleted"] = True
        _atomic_write_json(GROUPS_FILE, out)
    return {"ok": True, "cover": cv}


@app.patch("/api/courses/{cid}/pagebg")
async def set_page_bg(cid: str, request: Request):
    """页面背景深浅: 连续值 0-100(不再是 0-4 五档)。存数字, 与旧版字符串档位区分。"""
    raw = (await request.json()).get("page_bg", 0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "背景深浅必须是 0-100 的数字")
    if not (0 <= v <= 100):
        raise HTTPException(400, "背景深浅仅支持 0-100")
    v = round(v, 2)
    meta = load_meta(cid)
    meta["page_bg"] = v
    save_meta(cid, meta)
    return {"ok": True, "page_bg": v}


@app.patch("/api/courses/{cid}/pagebgimg")
async def set_page_bg_img(cid: str, request: Request):
    """课堂页底图: 传图片地址则启用, 传 null/空串则清除(回到纯色档位)。"""
    u = (await request.json()).get("url")
    meta = load_meta(cid)
    if u in (None, ""):
        meta.pop("page_bg_img", None)
        v = None
    else:
        if not isinstance(u, str) or not re.match(r"^/api/cover/[0-9a-f]{32}\.jpg$", u):
            raise HTTPException(400, "底图地址无效")
        meta["page_bg_img"] = u
        v = u
    save_meta(cid, meta)
    return {"ok": True, "page_bg_img": v}



@app.post("/api/courses/{cid}/ppt")
def upload_ppt(cid: str, file: UploadFile = File(...)):
    meta = load_meta(cid)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pptx", ".pdf"):
        raise HTTPException(400, "仅支持 .pptx 或 .pdf")
    # 原件统一存进「参考文件」, 用原始文件名(看得懂); 预览用的 PDF 走 .preview/ppt/view.pdf
    files = _files_dir(cid)
    old = _file_entry(meta, "ppt")
    if old and old.get("stored"):
        try:
            (files / old["stored"]).unlink()
        except OSError:
            pass
    raw = files / _safe_stored_name(file.filename or f"slides{suffix}", suffix, files)
    raw.write_bytes(file.file.read())

    slides = extract_pptx(raw) if suffix == ".pptx" else extract_pdf(raw)
    slides_text = "\n\n".join(f"--- 第{s['idx']}页 ---\n{s['text']}" for s in slides if s["text"])

    meta.update({"has_ppt": True,
                 "slides": [{"idx": s["idx"],
                             "title": (s["text"].split("\n")[0][:40] if s["text"] else "")}
                            for s in slides],
                 "slides_text": slides_text, "ppt_file": raw.name})
    ent = _file_entry(meta, "ppt")
    if not ent:
        meta.setdefault("files", []).insert(0, {"fid": "ppt", "name": raw.name, "kind": "ppt",
                                                "size": raw.stat().st_size, "mtime": int(raw.stat().st_mtime),
                                                "source": "ppt", "stored": raw.name})
    else:
        ent.update({"name": raw.name, "kind": "ppt", "size": raw.stat().st_size,
                    "mtime": int(raw.stat().st_mtime), "stored": raw.name})
    save_meta(cid, meta)

    # 热词/大纲改为后台抽取: 上传立刻返回, 页面可随时离开
    _start_terms_job(cid, meta.get("name") or "")
    shutil.rmtree(_preview_dir(cid, "ppt", create=False), ignore_errors=True)

    # 预览用 PDF: pptx 走 LibreOffice 转换(缓存在 .preview/ppt/view.pdf); pdf 直接复用原件
    try:
        if suffix == ".pdf":
            _preview_dir(cid, "ppt").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw, _preview_dir(cid, "ppt") / "view.pdf")
        else:
            _convert_to_pdf(cid, "ppt", raw)
    except Exception:
        pass
    meta["terms_job"] = "running"
    return meta


@app.post("/api/courses/{cid}/chunks/{idx}")
def upload_chunk(cid: str, idx: int, file: UploadFile = File(...)):
    ck = cdir(cid) / ".work" / "chunks"
    ck.mkdir(parents=True, exist_ok=True)
    (ck / f"rec_{idx:04d}.webm").write_bytes(file.file.read())
    return {"ok": True, "idx": idx}


# ---------- 流式实时转写 / 多次上课 ----------

# 合成状态: cid -> {sid: {"state": "synthesizing|ready|failed", "error": str}}
AUDIO_SYNTH: dict = {}
AUDIO_SYNTH_LOCK = threading.Lock()


def _set_synth(cid: str, sid: int, state: str, error: str = ""):
    with AUDIO_SYNTH_LOCK:
        AUDIO_SYNTH.setdefault(cid, {})[sid] = {"state": state, "error": error}


def _get_synth(cid: str, sid: int) -> dict:
    with AUDIO_SYNTH_LOCK:
        return AUDIO_SYNTH.get(cid, {}).get(sid) or {"state": "", "error": ""}


def _expected_audio_len(work: Path, sid: int) -> float:
    """这场课应有的音频长度(秒): 实时 PCM 每块 20 秒, 用块数估算。"""
    live = work / "live" / f"s{sid}"
    if not live.is_dir():
        return 0.0
    try:
        n = len(list(live.glob("live_*.wav")))
    except OSError:
        return 0.0
    return max(0.0, n * 20.0 - 6.0)


def _synth_session_audio(cid: str, sids: list[int]):
    """按场合成回放 mp3: 每场独立容错, 单场失败不影响其他场, 状态可查。"""
    d = cdir(cid)
    work = d / ".work"
    for sid in sids:
        try:
            out = audio_path(cid, sid)
            # 已有成品: 但如果比"应有的长度"明显短(之前误判下课, 只合成了开头一小段),
            # 重新合成一次, 否则用户听到的就是残缺音频
            if out.is_file() and out.stat().st_size > 0:
                exp = _expected_audio_len(work, sid)
                cur = _dur_of(out)
                if exp <= 1 or cur >= exp * 0.85:
                    _set_synth(cid, sid, "ready")
                    continue
                print(f"[audio] sid={sid} 现有音频 {round(cur,1)}s < 应有 {round(exp,1)}s, 重新合成",
                      flush=True)
            _set_synth(cid, sid, "synthesizing")
            src, _src_type = _session_wav(work, sid)
            if not src:
                msg = "没有可用录音源"
                print(f"[audio] sid={sid} {msg}, 跳过", file=sys.stderr, flush=True)
                _set_synth(cid, sid, "failed", msg)
                continue
            r = run_quiet([FFMPEG, "-v", "error", "-y", "-i", str(src),
                            "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)],
                           capture_output=True)
            if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
                err = (r.stderr or b"").decode("utf-8", "ignore")[:200] or "ffmpeg 合成失败"
                print(f"[audio] sid={sid} 合成失败: {err}", file=sys.stderr, flush=True)
                _set_synth(cid, sid, "failed", err)
                continue
            _set_synth(cid, sid, "ready")
        except Exception as e:
            print(f"[audio] sid={sid} 合成异常: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            _set_synth(cid, sid, "failed", f"{type(e).__name__}: {e}")
    try:
        _archive_sync(cid, ["录音原稿"])
    except Exception:
        pass


def _missing_audio_sids(cid: str) -> list[int]:
    """需要补合成的场次: 没有回放音频, 或音频明显比录到的内容短。"""
    meta = load_meta(cid)
    d = cdir(cid)
    work = d / ".work"
    out = []
    for s in meta.get("sessions") or []:
        sid = s.get("sid")
        if sid is None:
            continue
        has_data = (work / "sessions" / f"s{sid}").is_dir() or (work / "live" / f"s{sid}").is_dir()
        if not has_data:
            continue
        p = audio_path(cid, sid)
        if not p.is_file() or p.stat().st_size == 0:
            out.append(sid)
            continue
        exp = _expected_audio_len(work, sid)
        if exp > 1 and _dur_of(p) < exp * 0.85:
            out.append(sid)
    return out


def _resume_missing_audio():
    """启动时补合成缺失的回放音频(上次合成被重启打断时自愈)。"""
    for cid, _d in list(iter_class_dirs()):
        try:
            missing = _missing_audio_sids(cid)
        except Exception:
            continue
        if not missing:
            continue
        print(f"[audio] {cid} 补合成缺失音频: {missing}", flush=True)
        threading.Thread(target=_synth_session_audio, args=(cid, missing), daemon=True).start()


@app.get("/api/courses/{cid}/audio_status")
def audio_status(cid: str):
    """每场录音的回放状态: ready / synthesizing / failed / missing。"""
    meta = load_meta(cid)
    out = []
    for s in meta.get("sessions") or []:
        sid = s.get("sid")
        if sid is None:
            continue
        p = audio_path(cid, sid)
        st = _get_synth(cid, sid)
        if p.is_file() and p.stat().st_size > 0:
            state, err = "ready", ""
        elif st.get("state") == "synthesizing":
            state, err = "synthesizing", ""
        elif st.get("state") == "failed":
            state, err = "failed", st.get("error") or ""
        else:
            state, err = "missing", ""
        out.append({"sid": sid, "label": s.get("label", sid + 1),
                    "started": s.get("started", ""), "ended": s.get("ended"),
                    "state": state, "error": err,
                    "sec": round(_dur_of(p), 1) if state == "ready" else 0})
    return out


@app.post("/api/courses/{cid}/begin")
def begin_session(cid: str):
    with META_LOCK:
        meta = load_meta(cid)
        # 自动封存上次未正常结束的session, 每次点"上课"都是新的一次课
        for s in meta.get("sessions", []):
            if s.get("ended") is None:
                s["ended"] = datetime.now().isoformat(timespec="seconds")
                s["stale"] = True
        sids = [s["sid"] for s in meta.get("sessions", [])]
        sid = max(sids) + 1 if sids else 0
        meta.setdefault("sessions", []).append(
            {"sid": sid, "label": sid + 1, "started": datetime.now().isoformat(timespec="seconds"),
             "ended": None})
        meta["status"] = "recording"
        meta["live_transcript"] = (meta.get("live_transcript") or "") + f"【第{sid + 1}次上课】\n"
        save_meta(cid, meta)
        # 上次没点「下课」就直接点「上课」时, 旧场次不会自动出音频; 这里补合成缺失的回放文件
        missing = [s["sid"] for s in meta.get("sessions") or []
                   if s["sid"] != sid and not audio_path(cid, s["sid"]).is_file()]
    (cdir(cid) / ".work" / "sessions" / f"s{sid}").mkdir(parents=True, exist_ok=True)
    (cdir(cid) / ".work" / "live" / f"s{sid}").mkdir(parents=True, exist_ok=True)
    if missing:
        threading.Thread(target=_synth_session_audio, args=(cid, missing), daemon=True).start()
    return {"sid": sid}


@app.post("/api/courses/{cid}/chunks/{sid}/{idx}")
def upload_chunk(cid: str, sid: int, idx: int, file: UploadFile = File(...)):
    ck = cdir(cid) / ".work" / "sessions" / f"s{sid}"
    ck.mkdir(parents=True, exist_ok=True)
    (ck / f"rec_{idx:04d}.webm").write_bytes(file.file.read())
    return {"ok": True, "idx": idx}


@app.post("/api/courses/{cid}/live/{sid}/{start}")
async def live_transcribe(cid: str, sid: int, start: int, request: Request, w: str = ""):
    """接收 16k 单声道 s16le 裸PCM, 包上wav头后实时转写; w=真实时钟HH:MM"""
    import io
    import wave
    pcm = await request.body()
    if len(pcm) < 3200:
        return {"text": ""}
    meta = load_meta(cid)
    wav = cdir(cid) / ".work" / "live" / f"s{sid}" / f"live_{start:06d}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    wav.write_bytes(buf.getvalue())
    try:
        text = qwen_asr(wav, build_context(meta)).strip()
    except Exception as e:
        return {"text": "", "error": str(e)[:200]}
    if text:
        # 时间戳用「本次课内的相对时间」(mm:ss), 与音频播放进度一一对应。
        # 以前优先用前端传来的墙上时钟 w(HH:MM), 看录音原稿时对不上音频位置。
        ts = fmt_ts(start)
        cur = meta.get("live_transcript") or ""
        meta["live_transcript"] = (cur + f"[{ts}] {text}" + chr(10)).strip() + chr(10)
        save_meta(cid, meta)
    return {"text": text, "start": start}


def _session_data_mtime(cid: str, sid: int) -> float:
    """这场课最近一次录音数据落盘的时间(用于判断是不是还在录)。"""
    work = cdir(cid) / ".work"
    latest = 0.0
    for sub in (work / "live" / f"s{sid}", work / "sessions" / f"s{sid}"):
        if not sub.is_dir():
            continue
        try:
            for p in sub.iterdir():
                if p.is_file():
                    latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    return latest


@app.post("/api/courses/{cid}/stop/{sid}")
def stop_session(cid: str, sid: int):
    """下课: 转写已经实时完成, 这里只封存session并合成回放音频, 不做任何AI整理

    注意: 有些"下课"是浏览器后台节流导致的误判(心跳断了, 其实还在录)。
    所以先等 45 秒: 这期间若还有新录音块上传, 说明还在上课, 取消这次封存。
    """
    import datetime as _dt
    _defer_stop(cid, sid)
    return {"ok": True, "sid": sid, "deferred": True}


def _finalize_stop(cid: str, sid: int):
    """真正封存这一场并合成回放音频(延后 45 秒后调用)。"""
    import datetime as _dt
    meta = load_meta(cid)
    total = 0.0
    for s in meta.get("sessions", []):
        if s["sid"] == sid and s.get("ended") is None:
            s["ended"] = datetime.now().isoformat(timespec="seconds")
        if s.get("started") and s.get("ended"):
            try:
                a = _dt.datetime.fromisoformat(s["started"])
                b = _dt.datetime.fromisoformat(s["ended"])
                total += (b - a).total_seconds()
            except ValueError:
                pass
    meta["duration"] = int(total)
    # 实时转写即最终转写
    if meta.get("live_transcript"):
        meta["transcript"] = meta["live_transcript"]
    meta["status"] = "recorded"   # 转写已实时完成, 等待手动整理
    meta["recorded_at"] = datetime.now().isoformat(timespec="seconds")
    save_meta(cid, meta)

    # 后台只合成刚下课时这一场(以前会把所有场次重算一遍, 后面几场迟迟出不来);
    # 前端轮询 /audio_status 拿进度
    _set_synth(cid, sid, "synthesizing")
    threading.Thread(target=_synth_session_audio, args=(cid, [sid]), daemon=True).start()
    return {"ok": True, "sid": sid}


def _defer_stop(cid: str, sid: int, wait: float = 45.0):
    """延后封存: 等 wait 秒, 期间若还有新录音块上传 -> 还在录, 放弃封存。"""
    def run():
        base = _session_data_mtime(cid, sid)
        time.sleep(wait)
        try:
            if _session_data_mtime(cid, sid) > base + 1:
                # 还有新数据上传 -> 其实还在上课, 不封存(前端会自动再发 stop)
                print(f"[stop] {cid} s{sid} 仍有新录音, 取消封存", flush=True)
                return
            _finalize_stop(cid, sid)
        except Exception as e:
            print(f"[stop] {cid} s{sid} 延后封存失败: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, name="defer-stop", daemon=True).start()


@app.post("/api/courses/{cid}/del_session/{sid}")
def del_session(cid: str, sid: int, label: int = 0):
    """删除某次录音: 音频/PCM/录音块/对应转写稿段落一并清除"""
    import re as _re
    d = cdir(cid)
    meta = load_meta(cid)
    shutil.rmtree(d / ".work" / "live" / f"s{sid}", ignore_errors=True)
    shutil.rmtree(d / ".work" / "sessions" / f"s{sid}", ignore_errors=True)
    audio_path(cid, sid).unlink(missing_ok=True)
    with AUDIO_SYNTH_LOCK:
        AUDIO_SYNTH.get(cid, {}).pop(sid, None)
    meta["sessions"] = [s for s in meta.get("sessions", []) if s["sid"] != sid]
    # 从转写稿中摘除该次上课的段落
    t = meta.get("live_transcript") or ""
    if t and label > 0:
        parts = _re.split(r"(【第\d+次上课】[^\n]*\n?)", t)
        out, skip = [], False
        for seg in parts:
            m = _re.match(r"【第(\d+)次上课】", seg)
            if m:
                skip = (int(m.group(1)) == label)
                if not skip:
                    out.append(seg)
            elif not skip and seg:
                out.append(seg)
        meta["live_transcript"] = "".join(out).strip()
        meta["transcript"] = meta["live_transcript"]
    save_meta(cid, meta)
    return {"ok": True}


# ---------- 上课中的实时要点(滚动摘要) ----------
# 首次用当前这次课的转写生成小结; 之后每次只送「上一版小结 + 这100秒新增转写」,
# 不再回看大段历史转写。live_notes / live_notes_upto / live_notes_sid 一起落盘到 meta.json。
LIVE_NOTES_SYS = "你是课堂实时笔记助手，只输出简体中文 Markdown 要点。"
# 该供应商的默认思考模式会把 900 token 全花在推理上、拿不到正文; 实时要点只要条目, 关掉思考
LIVE_NOTES_EXTRA = {"thinking": {"type": "disabled"}}
LIVE_NOTES_SLIDES_CHARS = 2500   # 首次摘要参考的 PPT 文字上限
LIVE_NOTES_TAIL_CHARS = 6000     # 首次摘要参考的转写上限(与旧实现一致)


def _live_notes_plan(meta: dict) -> dict:
    """算这次要送模型的增量: 同一节课内只取上次摘要之后新追加的转写。"""
    live = meta.get("live_transcript") or ""
    heads = list(re.finditer(r"【第(\d+)次上课】", live))
    sid = int(heads[-1].group(1)) - 1 if heads else None
    start = heads[-1].end() if heads else 0
    prev = (meta.get("live_notes") or "").strip()
    upto = meta.get("live_notes_upto")
    same = (bool(prev) and meta.get("live_notes_sid") == sid
            and isinstance(upto, int) and start <= upto <= len(live))
    inc = live[(upto if same else start):]
    inc = re.sub(r"【第\d+次上课】[^\n]*\n?", "", inc).strip()
    if len(inc) > LIVE_NOTES_TAIL_CHARS:
        inc = inc[-LIVE_NOTES_TAIL_CHARS:]
    return {"sid": sid, "prev": prev, "increment": inc,
            "first": (not same) or not prev, "upto": len(live)}


def _live_notes_prompt(plan: dict, slides: str) -> str:
    inc = plan["increment"]
    if plan["first"]:
        return f"""下面是这节课到目前为止的实时转写稿（每20秒一段，可能有少量错字）。请用简体中文 Markdown 输出目前已经讲到的要点：
- 以 "## 本课要点" 开头
- 最多 10 条，按讲述顺序排列
- 保留关键定义、公式、例子；不要编造没讲到的内容
- 每条可带时间点 [mm:ss]

--- PPT 文字（可能为空） ---
{slides or "（本课未上传PPT）"}

--- 实时转写 ---
{inc}"""
    return f"""下面是这节课上一版的要点小结，以及最近约100秒新增的转写。请输出更新后的完整要点：
- 以 "## 本课要点" 开头
- 最多 10 条（硬性要求，绝不能出现第 11 条）
- 较早的内容压缩成 1~2 条概括（例如「前期回顾：……」），把条数留给最新内容
- 合并重复内容、修正明显的错字；保留关键定义、公式、例子；不要编造没讲到的内容
- 每条可带时间点 [mm:ss]

--- 上一版要点 ---
{plan["prev"]}

--- 新增转写（约100秒） ---
{inc}"""


def _looks_like_notes(txt: str) -> bool:
    """模型只吐推理过程时不把它当成要点: 必须有 "## 本课要点" 标题和至少两条条目。"""
    if not txt:
        return False
    if not re.search(r"^#+\s*本课要点\s*$", txt, re.M):
        return False
    return len(re.findall(r"^\s*(?:-|\d+\.)\s+\S", txt, re.M)) >= 2


@app.post("/api/courses/{cid}/live_organize")
def live_organize(cid: str):
    """上课中的实时要点: 每100秒由前端调用一次, 走滚动摘要(上一版小结 + 新增转写)"""
    meta = load_meta(cid)
    plan = _live_notes_plan(meta)
    prev = plan["prev"]
    if not plan["increment"]:
        # 这100秒没有新转写: 不调用模型, 直接返回上一版
        return {"notes": prev, "skipped": True}
    prompt = _live_notes_prompt(plan, (meta.get("slides_text") or "")[:LIVE_NOTES_SLIDES_CHARS])
    try:
        notes = (llm_chat(prompt, LIVE_NOTES_SYS, max_tokens=1200, extra=LIVE_NOTES_EXTRA) or "").strip()
    except Exception as e:
        # 失败不覆盖上一版: meta 里的 live_notes 原样保留
        return {"notes": prev, "error": str(e)[:200]}
    if not _looks_like_notes(notes):
        return {"notes": prev, "error": "模型未返回有效要点，已保留上一版"}
    with META_LOCK:
        # 重新读一次, 避免把摘要生成期间新追加的转写覆盖掉
        cur = load_meta(cid)
        cur["live_notes"] = notes
        cur["live_notes_sid"] = plan["sid"]
        cur["live_notes_upto"] = plan["upto"]
        save_meta(cid, cur)
    return {"notes": notes}


@app.post("/api/courses/{cid}/organize")
def organize_course(cid: str):
    """手动触发: 根据录音整理按页讲稿"""
    meta = load_meta(cid)
    if not (meta.get("transcript") or meta.get("live_transcript")):
        raise HTTPException(400, "还没有可整理的转写内容, 先上课")
    meta["status"] = "organizing"
    meta["error"] = ""
    save_meta(cid, meta)
    threading.Thread(target=run_organize, args=(cid,), daemon=True).start()
    return {"ok": True}


@app.post("/api/courses/{cid}/transcript/review")
def review_transcript(cid: str):
    """手动触发一次原稿 AI 复审(后台分块执行, 原稿不变)。
    状态/进度/结果随 GET /api/courses/{cid} 返回, 前端轮询即可。"""
    meta = load_meta(cid)
    if not (meta.get("live_transcript") or meta.get("transcript")):
        raise HTTPException(400, "还没有录音原稿可复审")
    if meta.get("review_status") == "running":
        return {"ok": True, "status": "running"}
    meta["review_status"] = "running"
    meta["review_progress"] = "0/?"
    meta.pop("review_error", None)
    save_meta(cid, meta)
    threading.Thread(target=run_transcript_review, args=(cid,), daemon=True).start()
    return {"ok": True, "status": "running"}


@app.post("/api/courses/{cid}/rerun")
def rerun(cid: str):
    meta = load_meta(cid)
    meta["status"] = "organizing"
    meta["error"] = ""
    save_meta(cid, meta)
    threading.Thread(target=run_organize, args=(cid,), daemon=True).start()
    return {"ok": True}


@app.get("/api/courses/{cid}/audio/{sid}")
def get_audio(cid: str, sid: int):
    f = audio_path(cid, sid)
    if not f.is_file():
        raise HTTPException(404, "audio not ready")
    return FileResponse(f, media_type="audio/mpeg")


def _last_session_text(live: str) -> str:
    """live_transcript 是多次上课累计的, 这里只取最近一次课的正文(去掉【第N次上课】标题)。"""
    text = (live or "").strip()
    if not text:
        return ""
    parts = re.split(r"【第\d+次上课】", text)
    return (parts[-1] if parts else text).strip()


SEARCH_TIMEOUT = 15.0


class SearchUnavailable(RuntimeError):
    pass


class SearchFailed(RuntimeError):
    def __init__(self, msg: str, status: str = "error"):
        super().__init__(msg)
        self.status = status


def web_search(query: str) -> list:
    """Tavily 联网搜索。key 来自 config.json 的 tavily_api_key 或环境变量 TAVILY_API_KEY。"""
    cfg = load_config()
    key = (cfg.get("tavily_api_key") or os.environ.get("TAVILY_API_KEY") or "").strip()
    if not key:
        raise SearchUnavailable('未配置联网搜索：请在 config.json 填 "tavily_api_key"'
                                '（或设置环境变量 TAVILY_API_KEY）后重试。')
    try:
        r = httpx.post("https://api.tavily.com/search",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"query": query[:400], "max_results": 5,
                             "search_depth": "basic", "include_answer": False},
                       timeout=SEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        detail = ""
        try:
            detail = str((e.response.json() or {}).get("detail") or "")[:120]
        except Exception:
            detail = (e.response.text or "")[:120]
        if code == 401:
            raise SearchFailed("联网搜索 key 无效（401），请检查 config.json 的 tavily_api_key。", "auth") from e
        if code == 403:
            raise SearchFailed(f"联网搜索被拒绝（403）：key 无权限或套餐受限。{detail}", "auth") from e
        if code in (429, 432):
            raise SearchFailed(f"联网搜索额度或频率受限（{code}），请稍后再试或检查套餐额度。{detail}", "quota") from e
        raise SearchFailed(f"联网搜索失败（HTTP {code}）：{detail}", "error") from e
    except httpx.TimeoutException as e:
        raise SearchFailed(f"联网搜索超时（{SEARCH_TIMEOUT:g} 秒），下面回答未使用联网资料。",
                           "timeout") from e
    except Exception as e:
        raise SearchFailed(f"联网搜索失败：{str(e)[:140]}", "error") from e
    out = []
    for item in (data.get("results") or [])[:5]:
        url = str(item.get("url") or "")
        if not re.match(r"^https?://", url):
            continue
        out.append({"title": (item.get("title") or url)[:120], "url": url,
                    "snippet": re.sub(r"\s+", " ", item.get("content") or "")[:400]})
    return out


# ---------- 联网搜索作为 function calling 工具 ----------
MAX_SEARCH_CALLS = 2   # 单轮问答内最多真正检索几次
SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": ("联网搜索互联网。仅在用户明确要求联网/搜索、问题涉及时效性信息、"
                        "或本课资料不足以回答且需要外部事实查证时调用；普通概念讲解不要调用。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "简洁明确的搜索关键词"}},
            "required": ["query"],
        },
    },
}


def search_tool_result(query: str) -> dict:
    try:
        return {"results": web_search(query)}
    except SearchUnavailable as e:
        return {"error": str(e), "status": "no_key"}
    except SearchFailed as e:
        return {"error": str(e), "status": e.status}
    except Exception as e:
        return {"error": f"联网搜索失败：{str(e)[:120]}", "status": "error"}


def llm_chat_with_tools(messages: list, system: str, tools: list, on_tool,
                        max_calls: int = MAX_SEARCH_CALLS, max_tokens: int = 2500):
    """OpenAI 兼容的 tools 自行调用; 返回 (最终回答, 实际检索次数)。

    模型自己决定是否调用 web_search；超过 max_calls 后不再提供工具，强制它直接作答。
    """
    errors = []
    for name, url, key, model in llm_providers():
        msgs = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in messages]
        calls = 0
        content = ""
        try:
            for _ in range(4):
                body = {"model": model, "messages": msgs, "temperature": 0.3,
                        "max_tokens": max_tokens}
                if calls < max_calls:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"
                r = httpx.post(url, json=body,
                               headers={"Authorization": f"Bearer {key}"}, timeout=300)
                r.raise_for_status()
                msg = (r.json().get("choices") or [{}])[0].get("message") or {}
                content = (msg.get("content") or "").strip()
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    return content, calls
                msgs.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": tcs})
                for tc in tcs:
                    fn = tc.get("function") or {}
                    nm = fn.get("name") or ""
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if nm != "web_search":
                        res = {"error": f"未知工具：{nm}"}
                    elif calls >= max_calls:
                        res = {"error": "本轮联网搜索次数已达上限，请基于已有信息直接回答"}
                    else:
                        calls += 1
                        res = on_tool(nm, str(args.get("query") or ""))
                        if isinstance(res, dict) and res.get("results"):
                            res = {"results": res["results"],
                                   "引用要求": "回答需在对应句末用 [1]、[2] 标注来源，"
                                               "不要粘贴链接（界面会单独展示来源列表）。"}
                    if not isinstance(res, dict):
                        res = {"error": str(res)[:200]}
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id") or f"call_{calls}",
                                 "content": json.dumps(res, ensure_ascii=False)[:6000]})
            return content, calls
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("所有LLM供应商均失败: " + " | ".join(errors))


@app.post("/api/courses/{cid}/chat")
async def course_chat(cid: str, request: Request):
    """课程问答: 带本课 PPT + 本场实时字幕/转写 + 可选联网搜索。"""
    payload = await request.json()
    msgs = payload.get("messages") or []
    meta = load_meta(cid)
    in_class = (meta.get("status") == "recording"
                or any(s.get("ended") is None for s in (meta.get("sessions") or [])))
    slides = (meta.get("slides_text") or "")[:4000]
    live = meta.get("live_transcript") or ""
    past = meta.get("transcript") or ""
    if in_class:
        # 上课中: 以本场实时字幕为主, 上次落盘的转写只给最近 2000 字作背景
        cur, label = _last_session_text(live)[-6000:], "本场实时字幕（最近 6000 字）"
        prev = past[-2000:] if past and past != live else ""
    else:
        cur, label = (past or live)[-6000:], "本课转写稿（最近 6000 字）"
        prev = ""
    persona = (load_config().get("assistant_persona") or ASSISTANT_PERSONA).strip()
    system = (persona + "\n\n" + ASSISTANT_PRIORITY +
              f"\n\n--- 本课 PPT（知识底座，可能为空） ---\n{slides or '（本课未上传 PPT）'}"
              f"\n\n--- {label} ---\n{cur or '（暂无字幕）'}")
    if prev:
        system += f"\n\n--- 上次课转写（仅作背景，最近 2000 字） ---\n{prev}"
    out = []
    for m in msgs[-12:]:
        role = m.get("role", "user")
        if role in ("user", "assistant"):
            out.append({"role": role, "content": m.get("content", "")})
    if not out or out[-1]["role"] != "user":
        out.append({"role": "user", "content": "帮我总结这节课"})
    last_user = out[-1]["content"]

    # 联网: 手动勾选 = 强制搜索; 否则把 web_search 注册为工具, 由模型自主决定是否调用
    force_search = bool(payload.get("web"))
    sources, notice, search_status = [], "", "off"
    used = False
    if force_search:
        used = True
        search_status = "ok"
        try:
            sources = web_search(last_user)
        except SearchUnavailable as e:
            search_status, notice = "no_key", str(e)
        except SearchFailed as e:
            search_status, notice = e.status, str(e)
        except Exception:
            search_status = "error"
            notice = "联网搜索失败或超时，下面回答未使用联网资料。"
        if sources:
            rows = [f"[{i}] {s['title']}\n链接：{s['url']}\n摘要：{s['snippet']}"
                    for i, s in enumerate(sources, 1)]
            system += ("\n\n--- 联网搜索结果（补充事实；结果冲突要说明；与 PPT/字幕冲突时以本课内容为准） ---\n"
                       + "\n\n".join(rows) +
                       "\n\n【引用要求】回答必须至少引用 [1] 和 [2]（结果不足 2 条则引用全部），"
                       "在对应句末用 [1]、[2] 标注；不要在正文重复粘贴链接（界面会单独展示可点击的来源列表）。")
        else:
            if not notice:
                search_status = "empty"
                notice = "没有搜到可靠结果，下面回答未使用联网资料。"
            system += ("\n\n【本次没有可用的联网结果】不要声称自己搜索过，也不要编造链接；"
                       "可以基于本课内容和常识回答，并说明未联网。")
        try:
            answer = llm_chat(chr(10).join(f"[{m['role']}] {m['content']}" for m in out),
                              system, max_tokens=2500)
        except Exception as e:
            return JSONResponse({"answer": "", "error": str(e)[:200], "sources": sources,
                                 "notice": notice,
                                 "search": {"used": True, "status": search_status,
                                            "count": len(sources)}}, status_code=200)
    else:
        system += ("\n\n【联网策略】本轮未强制联网。只有涉及时效性信息、外部事实或本课资料不足时，"
                   "才调用 web_search 工具；普通概念讲解不要搜索。若搜到结果，在对应句末用 [1][2] 标注来源，"
                   "不要粘贴链接；没搜到或未联网时，不要声称搜索过，也不要编造链接。")
        model_search = {"calls": [], "status": "off", "sources": [], "notice": ""}

        def on_tool(_name: str, q: str) -> dict:
            model_search["calls"].append(q)
            res = search_tool_result(q)
            if res.get("results"):
                model_search["status"] = "ok"
                seen = {s["url"] for s in model_search["sources"]}
                for s in res["results"]:
                    if s["url"] not in seen:
                        model_search["sources"].append(s)
                        seen.add(s["url"])
            elif res.get("status"):
                model_search["status"] = res["status"]
                model_search["notice"] = res.get("error") or ""
            return res

        try:
            answer, _n = llm_chat_with_tools(out, system, [SEARCH_TOOL], on_tool,
                                             max_calls=MAX_SEARCH_CALLS, max_tokens=2500)
        except Exception:
            # 供应商不支持 tools 等异常: 退回普通问答, 保证功能不中断
            try:
                answer = llm_chat(chr(10).join(f"[{m['role']}] {m['content']}" for m in out),
                                  system, max_tokens=2500)
            except Exception as e:
                return JSONResponse({"answer": "", "error": str(e)[:200], "sources": [],
                                     "notice": "",
                                     "search": {"used": False, "status": "off", "count": 0}},
                                    status_code=200)
        used = bool(model_search["calls"])
        sources = model_search["sources"]
        notice = model_search["notice"]
        search_status = model_search["status"] if used else "off"
    return {"answer": answer, "sources": sources, "notice": notice,
            "search": {"used": used, "status": search_status, "count": len(sources)}}


@app.get("/api/courses/{cid}/audios")
def list_audios(cid: str):
    """回放列表以磁盘上的音频文件为准: 有文件才出现, 删除后立即消失。"""
    d = cdir(cid)
    meta = load_meta(cid)
    info = {s["sid"]: s for s in meta.get("sessions") or []}
    out = []
    for ordn, sid in enumerate(_audio_sids(d), 1):
        s = info.get(sid) or {}
        out.append({"sid": sid, "ord": ordn, "label": s.get("label", sid + 1),
                    "started": s.get("started", ""),
                    "sec": round(_dur_of(d / "录音原稿" / f"audio_s{sid}.mp3"), 1),
                    "url": f"/api/courses/{cid}/audio/{sid}"})
    return out


@app.get("/api/courses/{cid}/slides_view")
def get_slides_view(cid: str):
    f = _files_dir(cid) / ".preview" / "ppt" / "view.pdf"
    if not f.is_file():
        raise HTTPException(404, "slides preview not ready")
    return FileResponse(f, media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.get("/api/courses/{cid}/pages")
def list_pages(cid: str):
    urls = get_page_urls(cid)
    d = cdir(cid)
    v = int((d / "pages").stat().st_mtime) if (d / "pages").is_dir() else 0
    return {"pages": urls, "v": v}


@app.get("/api/courses/{cid}/page/{name}")
def get_page(cid: str, name: str):
    fname = name if name.startswith("slide_") else f"slide_{name}"
    f = _files_dir(cid) / ".preview" / "ppt" / "pages" / fname
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


def _lookup_file(cid: str, fid: str):
    """课堂内找文件: 先课堂文件, 找不到再按课堂所属课程找课程文件。
    返回 (meta, entry, store_key, is_course); store_key 是渲染时用的目录键(cid 或 gid)。"""
    meta = load_meta(cid)
    ent = _file_entry(meta, fid)
    if ent:
        return meta, ent, cid, False
    gid = meta.get("group_id")
    if gid:
        cmeta = load_course_meta(gid)
        ce = _file_entry(cmeta, fid)
        if ce:
            return cmeta, ce, gid, True
    return meta, None, cid, False


def _course_file_list(gid: str) -> list:
    """课程文件列表(带页数), 条目一律标 source=course。"""
    meta = load_course_meta(gid)
    out = []
    for f in meta.get("files") or []:
        if not isinstance(f, dict) or not f.get("fid"):
            continue
        e = dict(f)
        e["source"] = "course"
        e["previewable"] = e.get("kind") in PAGED_KINDS | {"image", "md", "txt", "audio", "video"}
        if e.get("kind") == "pdf":
            e["pages"] = _pdf_count(_file_path(gid, meta, e))
        else:
            v = _preview_dir(gid, e["fid"], create=False) / "view.pdf"
            e["pages"] = _pdf_count(v) if v.is_file() else 0
        out.append(e)
    out.sort(key=lambda x: x.get("mtime") or 0)
    return out


@app.get("/api/courses/{cid}/files")
def list_course_files(cid: str):
    meta = load_meta(cid)
    if ensure_file_entries(cid, meta):
        save_meta(cid, meta)
    out = []
    for f in meta.get("files") or []:
        if not isinstance(f, dict) or not f.get("fid"):
            continue
        e = dict(f)
        e["source"] = "class"
        e["previewable"] = e.get("kind") in PAGED_KINDS | {"image", "md", "txt", "audio", "video"}
        if e.get("kind") == "ppt":
            e["pages"] = _pdf_count(_preview_dir(cid, "ppt", create=False) / "view.pdf")
        elif e.get("kind") == "pdf":
            e["pages"] = _pdf_count(_file_path(cid, meta, e))
        else:
            v = _preview_dir(cid, e["fid"], create=False) / "view.pdf"
            e["pages"] = _pdf_count(v) if v.is_file() else 0
        out.append(e)
    out.sort(key=lambda x: (0 if x.get("fid") == "ppt" else 1, x.get("mtime") or 0))
    # 课程文件在课堂里只读, 只参与预览
    gid = meta.get("group_id")
    if gid:
        for e in _course_file_list(gid):
            e["readonly"] = True
            out.append(e)
    return {"files": out, "use_course_files": bool(meta.get("use_course_files")),
            "group_id": gid}


@app.post("/api/courses/{cid}/files")
def upload_course_file(cid: str, file: UploadFile = File(...)):
    meta = load_meta(cid)
    ensure_file_entries(cid, meta)
    name = (file.filename or "").strip() or "未命名文件"
    name = name[:120]
    raw = file.file.read()
    if len(raw) > FILES_MAX_BYTES:
        raise HTTPException(413, "文件过大（上限 300MB）")
    kind = _file_kind(name)
    ext = Path(name).suffix.lower()
    files = _files_dir(cid)
    # 同名同类型 = 替换: 保留 fid, 覆盖原件并失效预览缓存, 避免出现两份同名文件
    old = next((f for f in meta.get("files") or []
                if isinstance(f, dict) and f.get("fid") != "ppt"
                and f.get("name") == name and f.get("kind") == kind), None)
    if old:
        fid = old["fid"]
        stored = _safe_stored_name(name, ext if len(ext) <= 10 else ".bin",
                                   files, keep=old.get("stored") or "")
        if old.get("stored") and old["stored"] != stored:
            try:
                (files / old["stored"]).unlink()
            except OSError:
                pass
        (files / stored).write_bytes(raw)
        old.update({"name": name, "kind": kind, "size": len(raw),
                    "mtime": int(time.time()), "stored": stored})
        shutil.rmtree(_preview_dir(cid, fid, create=False), ignore_errors=True)
        CONVERT_ERRORS.pop(fid, None)
        save_meta(cid, meta)
        _start_terms_job(cid, meta.get("name") or "")
        return {"ok": True, "file": old, "replaced": True, "terms_job": "running"}
    fid = uuid.uuid4().hex[:10]
    stored = _safe_stored_name(name, ext if len(ext) <= 10 else ".bin", files)
    (files / stored).write_bytes(raw)
    ent = {"fid": fid, "name": name, "kind": kind, "size": len(raw),
           "mtime": int(time.time()), "source": "upload", "stored": stored}
    meta.setdefault("files", []).append(ent)
    save_meta(cid, meta)
    _start_terms_job(cid, meta.get("name") or "")
    return {"ok": True, "file": ent, "terms_job": "running"}


@app.patch("/api/courses/{cid}/files/{fid}")
async def rename_course_file(cid: str, fid: str, request: Request):
    payload = await request.json()
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "文件名需为 1–120 个字符")
    meta = load_meta(cid)
    ent = _file_entry(meta, fid)
    if not ent:
        _m, _e, _k, is_course = _lookup_file(cid, fid)
        if is_course:
            raise HTTPException(403, "课程文件在课堂内只读，请到课程页的「课程文件」里改名")
        raise HTTPException(404, "文件不存在")
    # 磁盘上的原件用可读原名, 所以改名时磁盘文件名要跟着改
    if fid != "ppt" and ent.get("stored"):
        files = _files_dir(cid)
        old_p = files / ent["stored"]
        if old_p.is_file():
            ext = Path(name).suffix.lower() or Path(ent["stored"]).suffix.lower()
            new_stored = _safe_stored_name(name, ext if len(ext) <= 10 else ".bin", files)
            try:
                old_p.rename(files / new_stored)
                ent["stored"] = new_stored
                name = new_stored
            except OSError:
                pass
    ent["name"] = name
    save_meta(cid, meta)
    return {"ok": True, "file": ent}


def _file_trash_dir(cid: str, tag: str) -> Path:
    d = TRASH / f"{datetime.now():%Y%m%d-%H%M%S}-class-{cid}-file-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.delete("/api/courses/{cid}/files/{fid}")
def delete_course_file(cid: str, fid: str):
    """删除文件并移入 data/.trash(可恢复)。PPT 允许删除, 但保留 slides/slides_text/pages,
    因此讲稿与思维导图不受影响; 仅页图预览失效。"""
    meta = load_meta(cid)
    ent = _file_entry(meta, fid)
    if not ent:
        _m, _e, _k, is_course = _lookup_file(cid, fid)
        if is_course:
            raise HTTPException(403, "课程文件在课堂内只读，请到课程页的「课程文件」里删除")
        raise HTTPException(404, "文件不存在")
    dst = _file_trash_dir(cid, fid)
    if fid == "ppt":
        stored = (ent or {}).get("stored")
        if stored:
            p = _files_dir(cid) / stored
            if p.is_file():
                try:
                    shutil.move(str(p), str(dst / stored))
                except OSError:
                    pass
        # 旧版散落在课堂根的 slides.* 一并清走
        for name in ("slides.pdf", "slides.pptx", "slides.ppt", "slides_view.pdf"):
            p = cdir(cid) / name
            if p.is_file():
                try:
                    shutil.move(str(p), str(dst / name))
                except OSError:
                    pass
        meta["has_ppt"] = False
        meta.pop("ppt_file", None)
    else:
        p = _file_path(cid, meta, ent)
        if p and p.is_file():
            try:
                shutil.move(str(p), str(dst / p.name))
            except OSError:
                pass
    prev = _preview_dir(cid, fid, create=False)
    if prev.is_dir():
        try:
            shutil.move(str(prev), str(dst / "preview"))
        except OSError:
            shutil.rmtree(prev, ignore_errors=True)
    CONVERT_ERRORS.pop(fid, None)
    meta["files"] = [f for f in meta.get("files") or [] if f.get("fid") != fid]
    save_meta(cid, meta)
    return {"ok": True, "files": meta["files"]}


def _file_info_payload(key: str, meta: dict, ent: dict) -> dict:
    fid = ent.get("fid")
    kind = ent.get("kind")
    pages = 0
    err = None
    if kind in PAGED_KINDS:
        pages = _pdf_count(_entry_pdf(key, meta, ent))
        if not pages:
            err = CONVERT_ERRORS.get(fid) or "文件没有可显示的页面（可能为空或格式不受支持）"
    return {"fid": fid, "name": ent.get("name"), "kind": kind, "pages": pages,
            "previewable": kind in PAGED_KINDS | {"image", "md", "txt", "audio", "video"},
            "editable": kind in ("md", "txt") and fid != "ppt", "error": err}


@app.get("/api/courses/{cid}/files/{fid}/info")
def course_file_info(cid: str, fid: str):
    meta, ent, key, _is_course = _lookup_file(cid, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    return _file_info_payload(key, meta, ent)


@app.get("/api/courses/{cid}/files/{fid}/raw")
def course_file_raw(cid: str, fid: str):
    import mimetypes
    meta, ent, key, _is_course = _lookup_file(cid, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    p = _file_path(key, meta, ent)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return FileResponse(p, media_type=mime)


@app.get("/api/courses/{cid}/files/{fid}/content")
def course_file_content(cid: str, fid: str):
    meta, ent, key, _is_course = _lookup_file(cid, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    if ent.get("kind") not in ("md", "txt"):
        raise HTTPException(400, "该格式不支持文本预览")
    p = _file_path(key, meta, ent)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    return {"fid": fid, "name": ent.get("name"), "kind": ent.get("kind"),
            "content": _read_text_file(p), "size": p.stat().st_size}


@app.get("/api/courses/{cid}/files/{fid}/page/{n}")
def course_file_page(cid: str, fid: str, n: int, seg: int = 0):
    meta, ent, key, _is_course = _lookup_file(cid, fid)
    if not ent or ent.get("kind") not in PAGED_KINDS:
        raise HTTPException(404)
    cuts = None
    if seg:
        try:
            lay = json.loads((_preview_dir(key, fid, create=False) / "layout.json")
                             .read_text(encoding="utf-8"))
            entry = ((lay.get("pages") or {}).get(str(n)) or {})
            # 新格式: {rows:[], cols:[]}; 旧格式: {cuts:[float]} — 都支持
            if isinstance(entry, dict):
                if entry.get("rows") is not None or entry.get("cols") is not None:
                    cuts = entry
                elif entry.get("cuts") is not None:
                    cuts = entry["cuts"]
        except Exception:
            cuts = None
        if not cuts:
            seg = 0
    p = _render_page_jpg(key, meta, ent, n, seg, cuts)
    if not p:
        raise HTTPException(500, CONVERT_ERRORS.get(fid) or f"第 {n} 页渲染失败（文件可能为空或已损坏）")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/courses/{cid}/files/{fid}/layout")
def course_file_layout(cid: str, fid: str, detect: int = 0):
    meta, ent, key, _is_course = _lookup_file(cid, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    # 统一走 detect_layout: 内部按「文件 mtime-size + 算法版本」判断缓存,
    # 算法升级后无需用户手动点重新检测也能自动重切。
    return detect_layout(key, meta, ent, force=bool(detect))


# ---------- 课程文件库接口(课程 = groups.json 分组) ----------

def _require_gid(gid: str) -> str:
    if gid not in _group_ids():
        raise HTTPException(404, "course group not found")
    return gid


@app.get("/api/groups/{gid}/files")
def list_group_files(gid: str):
    _require_gid(gid)
    return {"files": _course_file_list(gid)}


@app.post("/api/groups/{gid}/files")
def upload_group_file(gid: str, file: UploadFile = File(...)):
    _require_gid(gid)
    meta = load_course_meta(gid)
    name = (file.filename or "").strip() or "未命名文件"
    name = name[:120]
    raw = file.file.read()
    if len(raw) > FILES_MAX_BYTES:
        raise HTTPException(413, "文件过大（上限 300MB）")
    kind = _file_kind(name)
    ext = Path(name).suffix.lower()
    old = next((f for f in meta.get("files") or []
                if isinstance(f, dict) and f.get("name") == name and f.get("kind") == kind), None)
    if old:
        fid = old["fid"]
        stored = fid + (ext if len(ext) <= 10 else ".bin")
        if old.get("stored") and old["stored"] != stored:
            try:
                (_files_dir(gid) / old["stored"]).unlink()
            except OSError:
                pass
        (_files_dir(gid) / stored).write_bytes(raw)
        old.update({"name": name, "kind": kind, "size": len(raw),
                    "mtime": int(time.time()), "stored": stored})
        shutil.rmtree(_preview_dir(gid, fid, create=False), ignore_errors=True)
        CONVERT_ERRORS.pop(fid, None)
        save_course_meta(gid, meta)
        return {"ok": True, "file": old, "replaced": True}
    fid = uuid.uuid4().hex[:10]
    stored = fid + (ext if len(ext) <= 10 else ".bin")
    (_files_dir(gid) / stored).write_bytes(raw)
    ent = {"fid": fid, "name": name, "kind": kind, "size": len(raw),
           "mtime": int(time.time()), "source": "course", "stored": stored}
    meta.setdefault("files", []).append(ent)
    save_course_meta(gid, meta)
    return {"ok": True, "file": ent}


@app.patch("/api/groups/{gid}/files/{fid}")
async def rename_group_file(gid: str, fid: str, request: Request):
    _require_gid(gid)
    payload = await request.json()
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "文件名需为 1–120 个字符")
    meta = load_course_meta(gid)
    ent = _file_entry(meta, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    ent["name"] = name
    save_course_meta(gid, meta)
    return {"ok": True, "file": ent}


def _group_file_trash_dir(gid: str, tag: str) -> Path:
    d = TRASH / f"{datetime.now():%Y%m%d-%H%M%S}-group-{gid}-file-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.delete("/api/groups/{gid}/files/{fid}")
def delete_group_file(gid: str, fid: str):
    _require_gid(gid)
    meta = load_course_meta(gid)
    ent = _file_entry(meta, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    dst = _group_file_trash_dir(gid, fid)
    p = _file_path(gid, meta, ent)
    if p and p.is_file():
        try:
            shutil.move(str(p), str(dst / p.name))
        except OSError:
            pass
    prev = _preview_dir(gid, fid, create=False)
    if prev.is_dir():
        try:
            shutil.move(str(prev), str(dst / "preview"))
        except OSError:
            shutil.rmtree(prev, ignore_errors=True)
    CONVERT_ERRORS.pop(fid, None)
    meta["files"] = [f for f in meta.get("files") or [] if f.get("fid") != fid]
    save_course_meta(gid, meta)
    return {"ok": True, "files": meta["files"]}


def _group_entry(gid: str, fid: str):
    meta = load_course_meta(gid)
    ent = _file_entry(meta, fid)
    if not ent:
        raise HTTPException(404, "文件不存在")
    return meta, ent


@app.get("/api/groups/{gid}/files/{fid}/info")
def group_file_info(gid: str, fid: str):
    _require_gid(gid)
    meta, ent = _group_entry(gid, fid)
    return _file_info_payload(gid, meta, ent)


@app.get("/api/groups/{gid}/files/{fid}/raw")
def group_file_raw(gid: str, fid: str):
    import mimetypes
    _require_gid(gid)
    meta, ent = _group_entry(gid, fid)
    p = _file_path(gid, meta, ent)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return FileResponse(p, media_type=mime)


@app.get("/api/groups/{gid}/files/{fid}/content")
def group_file_content(gid: str, fid: str):
    _require_gid(gid)
    meta, ent = _group_entry(gid, fid)
    if ent.get("kind") not in ("md", "txt"):
        raise HTTPException(400, "该格式不支持文本预览")
    p = _file_path(gid, meta, ent)
    if not p or not p.is_file():
        raise HTTPException(404, "文件不存在")
    return {"fid": fid, "name": ent.get("name"), "kind": ent.get("kind"),
            "content": _read_text_file(p), "size": p.stat().st_size}


@app.get("/api/groups/{gid}/files/{fid}/page/{n}")
def group_file_page(gid: str, fid: str, n: int, seg: int = 0):
    _require_gid(gid)
    meta, ent = _group_entry(gid, fid)
    if ent.get("kind") not in PAGED_KINDS:
        raise HTTPException(404)
    cuts = None
    if seg:
        try:
            lay = json.loads((_preview_dir(gid, fid, create=False) / "layout.json")
                             .read_text(encoding="utf-8"))
            entry = ((lay.get("pages") or {}).get(str(n)) or {})
            if isinstance(entry, dict):
                if entry.get("rows") is not None or entry.get("cols") is not None:
                    cuts = entry
                elif entry.get("cuts") is not None:
                    cuts = entry["cuts"]
        except Exception:
            cuts = None
        if not cuts:
            seg = 0
    p = _render_page_jpg(gid, meta, ent, n, seg, cuts)
    if not p:
        raise HTTPException(500, CONVERT_ERRORS.get(fid) or f"第 {n} 页渲染失败（文件可能为空或已损坏）")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/groups/{gid}/files/{fid}/layout")
def group_file_layout(gid: str, fid: str, detect: int = 0):
    _require_gid(gid)
    meta, ent = _group_entry(gid, fid)
    return detect_layout(gid, meta, ent, force=bool(detect))


@app.post("/api/ocr")
def ocr_image(file: UploadFile = File(...)):
    """PPT 区域截图 -> 文字/LaTeX (供剪贴板粘贴到笔记)"""
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "图片为空")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "图片过大（>10MB）")
    try:
        text = qwen_ocr(raw, file.content_type or "image/png")
    except Exception as e:
        return JSONResponse({"text": "", "error": f"{type(e).__name__}: {e}"[:300]},
                            status_code=200)
    return {"text": text}


# ---------- 我的笔记 (Markdown 文件) ----------

def notes_dir(cid: str) -> Path:
    d = cdir(cid) / "笔记"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    name = (name or "").strip().rstrip(".")
    if not name:
        raise HTTPException(400, "文件名不能为空")
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "文件名不合法")
    if not name.endswith(".md"):
        name += ".md"
    return name


@app.get("/api/courses/{cid}/mdfiles")
def list_md(cid: str):
    return sorted(p.stem for p in notes_dir(cid).glob("*.md"))


@app.get("/api/courses/{cid}/mdfile")
def read_md(cid: str, name: str):
    f = notes_dir(cid) / _safe_name(name)
    if not f.is_file():
        return {"name": _safe_name(name), "content": "", "new": True}
    return {"name": f.stem, "content": f.read_text(encoding="utf-8"), "new": False}


@app.post("/api/courses/{cid}/mdfile")
async def write_md(cid: str, request: Request):
    payload = await request.json()
    name = _safe_name(payload.get("name", ""))
    f = notes_dir(cid) / name
    f.write_text(payload.get("content", ""), encoding="utf-8")
    try: _archive_sync(cid, ["笔记"])
    except Exception: pass
    return {"ok": True, "name": f.stem}


@app.delete("/api/courses/{cid}/mdfile")
def delete_md(cid: str, name: str):
    f = notes_dir(cid) / _safe_name(name)
    f.unlink(missing_ok=True)
    try: _archive_sync(cid, ["笔记"])
    except Exception: pass
    return {"ok": True}


@app.post("/api/courses/{cid}/noteimg")
def upload_note_image(cid: str, file: UploadFile = File(...)):
    """笔记里粘贴/插入的图片，存到 notes/assets/"""
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "图片为空")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "图片过大（>10MB）")
    ct = (file.content_type or "").lower()
    ext = ".png"
    if "jpeg" in ct or "jpg" in ct:
        ext = ".jpg"
    elif "gif" in ct:
        ext = ".gif"
    elif "webp" in ct:
        ext = ".webp"
    d = notes_dir(cid) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    (d / name).write_bytes(raw)
    try: _archive_sync(cid, ["笔记"])
    except Exception: pass
    return {"url": f"/api/courses/{cid}/noteimg/{name}", "name": name}


@app.get("/api/courses/{cid}/noteimg/{name}")
def get_note_image(cid: str, name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "文件名不合法")
    f = notes_dir(cid) / "assets" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """前端页面禁用缓存: 改完刷新就生效, 不用手动清浏览器缓存。"""
    resp = await call_next(request)
    pth = request.url.path
    if pth in ("/", "/index.html") or pth.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("CC_PORT", "8321"))
    import uvicorn
    # 用 pythonw 启动时没有控制台, 把日志落到文件, 出问题还能查
    if sys.stdout is None or sys.stderr is None:
        try:
            (BASE / ".cache").mkdir(parents=True, exist_ok=True)
            _log = open(BASE / ".cache" / "runtime.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            _log = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or _log
        sys.stderr = sys.stderr or _log
    try:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except OSError:
        # 端口被占用 = 平台已经在运行, 直接打开页面即可
        print("平台已在运行, 正在打开页面...")
        webbrowser.open(f"http://127.0.0.1:{port}")
