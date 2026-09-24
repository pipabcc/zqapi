# zqapi 发布与开源核对清单

主仓库的开源总清单见 [OPEN_SOURCE_CHECKLIST.md](https://github.com/pipabcc/deepcat/blob/main/OPEN_SOURCE_CHECKLIST.md)，
本文件只针对 `zqapi/` 的增量事项。**提交/发布前按顺序过一遍。**

## 1. 发布前敏感自查（每次发版都要做）

```bash
# 1) 确认运行数据没进来（应输出空）
git status --short zqapi/data zqapi/dist zqapi/self-check zqapi/build

# 2) 确认没有硬编码密钥（应无输出）
grep -rn -E "sk-[a-z0-9]{8,}|AKID[0-9A-Za-z]{10,}|[a-f0-9]{40,}" \
  --include="*.py" --include="*.md" --include="*.yml" zqapi/

# 3) 确认 docs/ 截图里的 API Key 字段仍是圆点掩码（肉眼核对底部一行）
```

已核对过的项（2026-09-24）：

- [x] `data/`（含明文 Key 的数据库）在 `.gitignore` 里，未入库
- [x] 源码、README、tests 无硬编码密钥
- [x] `docs/screenshot-zhuque.png`：API Key 为圆点掩码，api 地址为公开默认值
- [x] `assets/help-edgeone-models.png`：无账号/密钥信息（站点名 `default-pages-zone` 可见，
      属 EdgeOne 默认名，介意可打码后重截）

## 2. 版本与打包

1. zqapi 目前**没有独立版本号**，随主仓库 Release 一起走（exe 版本 = 仓库 tag）。
   若要独立版本号，在 `main.py` 加 `APP_VERSION` 并写进 `--startup-probe` 输出。
2. 打包：先 `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`，
   然后 `.venv\Scripts\python.exe build_exe.py` → `dist/ZqApi.exe`（约 37.7 MB）。
3. 自检：`dist/ZqApi.exe --startup-probe` 与 `--self-check` 必须通过，且
   `dist/self-check/*.png` 与源码运行结果一致。
4. 计算 SHA-256（PowerShell）：`Get-FileHash dist\ZqApi.exe -Algorithm SHA256`。
5. 打 tag 并建 Release：上传 `ZqApi.exe` + SHA-256，Release 说明从
   `CHANGELOG.md` 的对应小节复制。

## 3. 仓库元数据建议

- **Description**：`AI写作工具箱 —— 朱雀 AI 文本检测与文本比较（PyQt6，Windows 单文件 exe）`
- **Topics**：`python` `pyqt6` `windows` `ai-detection` `ai-generated-text-detection`
  `text-diff` `text-comparison` `chinese` `sqlite`

## 4. 开源范围（已定：zqapi 单独开仓）

已按「`zqapi/` 单独抽成独立仓库」准备：本仓库自包含 LICENSE（GPL-3.0）、CI、README，
对主工程文档的引用改为 GitHub 链接。发布步骤：

1. 在 GitHub 建**空仓库**（不要初始化 README/License），建议名字 `zqapi`。
2. `git remote add origin https://github.com/pipabcc/zqapi.git && git push -u origin main`。
3. 按本文件打 tag、建 Release、上传 exe 与 SHA-256。

主仓库（deepcat）那边：`zqapi/` 目录保留为同一份源码，两边改动需要手动同步
（或者后续把 deepcat 里的 zqapi 换成 git submodule 指向独立仓库）。

其余公开前事项：

- deepcat 主仓库若也公开，还跟踪着 `ZHUQUE_REVIEW_2026-09-20.md`、`CHATGPT_WEB.md`、
  `HY_MT 本地指南`、`start_*_server` 脚本等内部文档，公开前要单独审一遍
  （它们已在 git 历史里，`.gitignore` 救不了）。
2. **`docs/code-review-2026-09-24.md` 是否随仓库公开**：内容是对代码问题的坦率描述
   （含"API Key 明文存储"等），公开是加分项，但发布前请自行决定去留。

## 5. 已就绪项（2026-09-24 检查）

- [x] `zqapi/` 源码、README（含下载/隐私/许可证/反馈章节）、tests、assets、docs 预览图入库
- [x] `.github/workflows/windows-ci.yml` 新增 `zqapi-tests` 任务（3.13，纯标准库测试）
- [x] `compileall` 覆盖 `zqapi/`，ruff 全绿（`Callable` 导入已补）
- [x] CHANGELOG 增加 zqapi 条目（"未发布"小节）
- [x] 根 README 增加「独立工具箱 zqapi」介绍
