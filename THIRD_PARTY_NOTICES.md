# Third-Party Notices

本项目在 `static/vendor/` 下内置了以下第三方前端库。它们各自保留原始版权与许可证，
在分发本项目时同样适用。

---

## CodeMirror

- **位置**：`static/vendor/codemirror/`
- **用途**：Markdown 编辑器（"我的笔记"）
- **许可证**：MIT
- **项目主页**：https://codemirror.net/
- **仓库**：https://github.com/codemirror/codemirror5

```
MIT License

Copyright (c) Marijn Haverbeke

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## KaTeX

- **位置**：`static/vendor/katex/`
- **用途**：公式渲染（讲稿 / 思维导图 / 笔记中的 LaTeX）
- **许可证**：MIT
- **项目主页**：https://katex.org/
- **仓库**：https://github.com/KaTeX/KaTeX

```
MIT License

Copyright (c) Khan Academy and other contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 运行时依赖（通过 pip 安装，不随本仓库分发）

| 包 | 许可证 |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| python-pptx | MIT |
| pypdf | BSD-3-Clause |
| PyMuPDF | AGPL-3.0（商业授权亦可） |
| Pillow | MIT-CMU |
| dashscope | Apache-2.0 |

> 注意：PyMuPDF 采用 AGPL-3.0 许可。若你的发行方式需要闭源，请改用其他 PDF 解析方案
> 或购买 PyMuPDF 商业授权。
