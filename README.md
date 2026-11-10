# Class Copilot · 课堂录音与讲课复现平台

> 上传 PPT → 一键录音 → 实时转写 → AI 对照 PPT 自动整理出**讲课复现讲稿 + 思维导图 + 实时要点**。
> 一堂课下来，课件、录音、字幕、讲稿、笔记、问答全部落在你自己的磁盘上，可回看、可检索、可复用。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [使用流程](#使用流程)
- [目录结构](#目录结构)
- [配置说明](#配置说明)
- [界面调节速查](#界面调节速查)
- [打包成桌面程序](#打包成桌面程序)
- [数据与隐私](#数据与隐私)
- [常见问题](#常见问题)
- [许可证](#许可证)

---

## 特性

| 能力 | 说明 |
|---|---|
| **PPT 自动解析** | 上传 `.pptx` / `.pdf`，自动提取**专业术语表**（作为转写热词）与**逐页大纲** |
| **课堂录音 + 实时转写** | 浏览器录音，每 60 秒自动落盘；字幕边说边出，带时间戳；**每 20 秒一段**，专有名词按术语表纠正 |
| **讲课复现** | 下课后 AI 对照 PPT 逐页改写转写稿，产出可直接当教案用的讲稿 |
| **思维导图** | 自动生成 Markdown 嵌套列表，一眼看清整堂课结构 |
| **实时要点** | 上课过程中滚动累积要点摘要，不用等下课 |
| **AI 复审** | 对照 PPT 修正转写稿里的错字与术语，产出更准的复审稿 |
| **预览区** | PPT/PDF 按页预览，版式检测自动把"一页两图"切成两段；支持适应/原始比例、取字(OCR)、下载原文件 |
| **多文件混排** | 课堂文件库 + 课程级公共文件库；Word/Excel/PPT 自动转 PDF 以便内嵌预览 |
| **我的笔记** | 内置 Markdown 编辑器，支持插图 |
| **AI 问答** | 就地问课堂内容，可选联网搜索并附来源；每次「清空」前自动留档 |
| **版式自由** | 三栏可折叠、可拖拽调宽、比例按课堂记忆；**全折叠后点一下背景即进入纯净模式**，画面只剩背景 |
| **进度记忆** | 预览翻阅位置按「场景 + 课堂 + 文件」分别记住，上课与下课互不干扰，F5 刷新也保留 |
| **纯本地** | 所有课程、录音、笔记都写在你自己磁盘上目录里，目录结构可读可备份 |

---

## 快速开始

### 系统要求

- Python **3.10+**
- [ffmpeg](https://ffmpeg.org/download.html)（录音解码与回放合成，需在 PATH 中）
  - Windows：`winget install Gyan.FFmpeg`
  - macOS：`brew install ffmpeg`
- [LibreOffice](https://www.libreoffice.org/download/)（可选，用于把 Word/Excel/PPT 转成 PDF 以便内嵌预览）

### 1. 安装依赖

```bash
git clone <this-repo>
cd class-copilot
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp config.example.json config.json     # Windows: copy config.example.json config.json
```

编辑 `config.json`：

```json
{
  "sensenova_key": "你的 SenseNova API Key",
  "sensenova_model": "sensenova-6.8-flash-lite",
  "tavily_api_key": "你的 Tavily API Key"
}
```

| Key | 用途 | 必填 |
|---|---|---|
| `sensenova_key` | AI 整理讲稿 / 思维导图 / 实时要点 / AI 复审 / 问答 | 建议填 |
| `sensenova_model` | 使用的模型名 | 有默认值 |
| `tavily_api_key` | AI 问答的联网搜索 | 可留空 |

语音转写使用阿里云百炼 `qwen3-asr`，通过环境变量提供：

```bash
# Windows
setx DASHSCOPE_API_KEY "你的 key"
# macOS / Linux
export DASHSCOPE_API_KEY="你的 key"
```

> 也支持用 `DEEPSEEK_API_KEY` 环境变量替代 `sensenova_key`（见 `app.py` 里的供应商优先级）。

### 3. 启动

```bash
# Windows
start.bat
# macOS / Linux
./run.sh
# 或者直接
python app.py
```

浏览器会自动打开 <http://127.0.0.1:8321>。想换端口：`set CC_PORT=9000` / `export CC_PORT=9000`。

---

## 使用流程

1. **新建课程**（如「信号与系统」），再在课程里**新建课堂**（如「第 3 章 时域校正」）。
2. **上传 PPT**：自动提取术语表与逐页大纲；热词抽取在后台跑，不阻塞页面。
3. **点「开始上课」**：录音 + 实时转写同时进行，每 60 秒自动保存，异常关页也不丢。
4. **点「下课」**：本场音频在后台合成，随后可在「录音原稿」页签按次回放。
5. **点「立即整理」**：AI 产出讲课复现讲稿、思维导图；「实时要点」上课过程中就已累积。
6. 之后随时回到「录音原稿 / 我的笔记 / 思维导图 / AI 问答」复习。

---

## 目录结构

```
项目根/
├─ app.py                 后端服务（FastAPI）
├─ desktop.py             桌面独立窗口入口（打包 exe 用）
├─ asr_terms.py           术语表处理
├─ static/  assets/       前端页面与图标
├─ config.json            你的配置（不进版本库）
├─ build/pyinstaller/     打包配置
│
├─ 课程库/                 ← 你的全部数据（唯一需要备份的目录）
│   └─ <课程名>/<课堂名>/
│        ├─ 参考文件/      上传的原件（PDF/Word/PPT/图片…），用原始文件名存放
│        │                  └─ .preview/  预览缓存（切好的页图、转换出的 PDF），可随时删
│        ├─ 笔记/          你自己写的 Markdown 笔记
│        ├─ AI整理/        AI 产物：讲课复现.md / 思维导图.md / 实时要点.md
│        ├─ 录音原稿/      每次上课的回放 mp3 + 转写文本（实时转写/整理后转写/AI复审稿）
│        ├─ AI问答/        当前会话 + 每次「清空」前的历史留档
│        └─ meta.json      课堂元信息（内部）
│
├─ .courses/ .covers/ .trash/ .groups.json .ui_state.json   ← 技术数据（. 前缀隐藏）
```

> 所有数据都在这个目录里，**删掉它就等于清空全部课程**，备份它即可迁移。

---

## 配置说明

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `CC_PORT` | 服务端口 | `8321` |
| `CC_BASE` | 数据根目录（课程库所在处） | 自动推断（源码目录或其上一级） |
| `DASHSCOPE_API_KEY` | 语音转写（qwen3-asr） | — |
| `DEEPSEEK_API_KEY` | 备用 LLM 供应商 | — |

---

## 界面调节速查

所有调节项**按课堂自动记住**，下次回来还是你调好的样子。

| 想调什么 | 在哪调 | 说明 |
|---|---|---|
| **块视图 / 窄视图** | 首页每个列表右上角的两个小按钮 | 课程区、课堂区、课程页**三个列表各自独立** |
| **栏折叠 / 展开** | 栏边缘的小箭头 | 上课页三栏（转写/预览/笔记）、下课页两栏都能收；**全收起后点一下背景 → 所有界面元素隐藏，只剩干净背景，再点一下回来**；展开任意一栏按钮自动回来 |
| **栏宽比例** | 拖动栏之间的分隔线 | 上课页默认 1 : 2 : 1，两栏时对半；比例按课堂分别记住；「恢复默认布局」可复位 |
| **页面底图** | 课堂页右上角「🖼 背景」→ 上传图片 | 每个课堂可单独设底图 |
| **背景深浅** | 顶栏右侧滑块 | 连续调节底图明暗，投影时看得清字 |
| **预览区不透明度** | 预览栏标题栏的滑块 | 预览半透明，不挡底图 |
| **预览缩放** | 预览栏「适应」按钮 | 「适应」= 按栏宽铺满；也可用「显示原页」看整页 |
| **整页 / 自动切分** | 预览区页脚「显示原页」 | 一页里含上下两张图时自动切成两段，可单独关掉某个文件的切分 |
| **PPT 取字** | 预览栏「✂ 取字」 | 对当前页做 OCR，识别结果可复制 |
| **预览翻阅位置** | 无需设置，自动记住 | 每个「场景 + 课堂 + 文件」各记一份：**上课中**和**下课后**互不干扰；F5 刷新也保留 |
| **记住所在页签** | 自动 | 讲课复现 / 录音原稿 / 我的笔记 |

---

## 打包成桌面程序

需要 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)（Windows 10/11 一般已内置）。

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm \
    --distpath dist \
    --workpath build/pyinstaller \
    build/pyinstaller/class-copilot.spec
```

产物在 `dist/class-copilot.exe`（单文件，双击即开独立窗口，无黑窗）。
若机器上没有 WebView2，会自动退化为 Edge 的应用窗口（同样无地址栏）。

> 打包时若报 `SHFileOperationW 失败`，说明旧 exe 被占用或删除受限——先把旧 exe 改名让开再打包。

---

## 数据与隐私

- **所有课程数据、录音、笔记、问答都只保存在你本机**，程序不上传任何课程内容。
- 调用外部服务的只有三类：**语音转写**（音频片段发往百炼）、**AI 整理/问答**（文本发往你配置的 LLM）、**可选的联网搜索**（问题发往 Tavily）。
- API Key 只存在本机 `config.json`，已被 `.gitignore` 排除。
- 想彻底清空：删除 `课程库/` 目录并重启。

---

## 常见问题

**Q：手机/平板能录吗？**
浏览器只在**安全上下文**里允许麦克风。`127.0.0.1` 本机没问题；用局域网 IP 访问（如 `http://192.168.x.x:8321`）麦克风会被禁用，需要 HTTPS。

**Q：转写里专业名词老是错？**
去「✏ 编辑热词」把术语补上（或换 PPT 重新提取），术语表会同时喂给转写和整理环节。

**Q：热词提取很慢？**
已关闭推理模型的 thinking 模式并改为后台抽取，正常在十几秒到一分钟内完成；上传完可以直接离开页面。

**Q：点「立即整理」没反应？**
看页面顶部横幅的状态（转写中 / 整理中）；也可能是 Key 没配或额度用完。

**Q：音频一直在转圈不出来？**
页面会显示「N 场音频正在后台合成，完成后自动出现」，失败会直接显示原因。服务重启后会自动补合成缺失或不完整的音频。

**Q：预览进度保不住？**
确认浏览器没有用旧版缓存（`Ctrl+F5`）；进度同时写在浏览器 localStorage 与服务端 `.ui_state.json`，两边都写，任一可用即可。

**Q：改了前端代码，页面没变化？**
按 `Ctrl+F5` 强刷一次。服务已设置 `Cache-Control: no-store`，之后刷新就是最新的。

**Q：端口被占用？**
`set CC_PORT=9000` 后再启动即可。

---

## 第三方组件

内置前端库（CodeMirror / KaTeX，均为 MIT）与运行时依赖的许可证说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

[MIT](LICENSE) © Class Copilot contributors
