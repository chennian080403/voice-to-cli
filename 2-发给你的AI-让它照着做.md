# 给 AI：在用户 Mac 上搭起「语音直连本机 AI」网页工作台

目标：在用户电脑上部署 `3-参考脚本（作者机器上在跑的，AI照着改）/` 里的本地语音直连服务（`server.py`、`index.html`、`config.json`、`语音` 启动命令），打通「浏览器麦克风 → 本地 SenseVoice 听写 → 发送到本机 CLI 前台会话 → MiMo 流式语音播报」全链路。

## 1. 先查清对方电脑

| 要弄清的 | 怎么查 / 不清楚怎么办 |
|---|---|
| 系统与工具 | 确认是 macOS；检查 `python3 --version`（≥ 3.10）、`ffmpeg -version` |
| 本地 SenseVoice 听写 | 检查是否已有 `sensevoice-asr`（或本机 SenseVoice 转写脚本，需提供 `transcribe(src_wav, out_dir, None, correction=False)` 并写出 `out_dir/transcript.txt`，及 `_ffmpeg_path()`）。没有就先帮用户按开源 SenseVoice-small + Python 搭一个本地听写入口，或修改 `server.py` 的 `transcribe()` 对接用户已有的本地 Whisper/SenseVoice |
| 前台会话遥控工具 | 查用户有没有 `~/bin/gbc` 或 `tmux` / `screen`。参考脚本通过 `gbc --<agent> send/busy/esc/key/wait` 操作独立后台终端会话；如果用户电脑上没有 `gbc`，用 `tmux` 写一个同等接口的小包装脚本即可（协议见第 4 节） |
| 已有哪些 CLI Agent | 检查 `claude`、`pi`、`kimi` 装了哪几个，调整 `server.py` / `config.json` 的 `agents` 与路径 |
| 密钥 | 让用户提供小米 MiMo API Key（存到 `~/.config/mimo/api.key`，`chmod 600`），和用于压缩长回复的 OpenAI 兼容接口地址与 Key（不强求本地网关，填用户现成的接口或省略压缩） |
| 端口占用 | 默认 `127.0.0.1:8780`，`lsof -nP -iTCP:8780 -sTCP:LISTEN` 检查，冲突就改 `config.json` 的 `port` |

## 2. 核心原理

1. **语音层只传话，不替主模型思考**：麦克风录音只负责转文字和念出第一段结论；所有理解、改代码、跑命令全交给用户电脑上真正干活的 CLI 会话。
2. **一代一号抢占机制（`GENERATION`）**：用户刚说一句话还没播完又按住开口时，全局递增 `GENERATION[0]`，旧一轮线程在 `_emit()` 检测到序号变化后立刻抛出 `StaleTurn` 停推音频，避免两句话声音重叠。
3. **预设短句内存预热**：服务启动时后台并行把 SenseVoice 模型预热（转 0.5 秒静音）并把「收到」「好，已打断」预合成为 PCM16 缓存在内存里，用户松手瞬间就能先听到「收到」反馈。
4. **独立会话名隔离**：默认使用 `gb-voice-pi` / `gb-voice-claude` / `gb-voice-kimi`，不撞用户在终端里自己开的其它会话；`语音 停` 只停掉 HTTP 服务，保留后台 AI 会话和上下文，下次接着聊。

## 3. 流程

```text
浏览器按住说话 → 16k WAV → POST /api/turn（SSE 流）
   ├─ 本地 SenseVoice 听写 + 热词纠正（hotwords）
   ├─ 命中「停 / 切到 X / 选 N」→ 直接发控制指令并语音回执
   └─ 其余原话 → 立即播「收到」+ 送入 CLI 会话 → 拿回复洗掉终端噪声
                 → 超 220 字交给轻量模型压成 80 字口语 → MiMo 逐句流式合成 PCM16 推回页面
```

## 4. 关键格式与接口约定

`3-参考脚本（作者机器上在跑的，AI照着改）/` 包含 4 个正本去痕迹副本：

| 文件 | 放哪 | 作用 |
|---|---|---|
| `1-服务端-server.py` | `~/voice-link/server.py` | 本地 HTTP + SSE 服务（仅用 Python 标准库） |
| `2-网页-index.html` | `~/voice-link/index.html` | 录音、免按键、Agent 切换与 PCM16 播放器前端 |
| `3-配置-config.json` | `~/voice-link/config.json` | 端口、热词表、会话名、语速、压缩模型配置 |
| `4-启动命令-语音` | `~/.local/bin/语音`（`chmod +x`） | 启 / 停 / 查状态命令 |

如果用户机器上没有现成 `gbc`，AI 用 `tmux` 实现一个满足以下 5 个子命令的包装脚本放到 `~/bin/gbc`（读取 `GBC_SESSION` 环境变量）：

| 调用形式 | 预期行为 |
|---|---|
| `gbc --<agent> send "<文本>" 600` | 若会话不存在则在该工作目录拉起对应 CLI；把文本送入提示词并等本轮输出结束，stdout 打印本轮最终回复；若弹确认框返回退出码 `3` 并打印选项内容 |
| `gbc --<agent> esc` | 向会话发送 `Escape` 中断当前任务 |
| `gbc --<agent> key <按键...>` | 逐个按键送入会话（如 `down enter` 或 `1`），等待输出后返回（若仍有确认框同样返回 `3`） |
| `gbc --<agent> busy` | 输出 `yes`、`no` 或 `ask` |
| `gbc --<agent> wait 4` | 在 `ask` 状态下返回当前确认框文本，退出码 `3` |

## 5. 实现细节（实测踩过的坑）

1. **热词纠偏要区分作用域**：SenseVoice 容易把英文「Claude」听成 `clot` / `克劳德`，可以全局替换；但把「派 / π」换成 `Pi` **绝对不能全局替换**，必须设 `"scope": "command"` 只在「切到 / 换到 / 用」后面换，否则平时说话里的「派发 / 苹果派」全会被改坏。
2. **不同 CLI 的确认框按法不一样**：Pi 的交互选择菜单没有数字快捷键，不能直接发 `1`，要拆成 `["down"] * (num - 1) + ["enter"]`；Claude Code 和 Kimi Code 直接发数字字符串即可（参考脚本 `server.py` 已内置该分支）。
3. **发送原话时附带口语提示词**：发给 AI 时在末尾拼 `PLACEHOLDER`（先用一两句口语说结论，别用列表和代码块开头），并在拿到回复后用 `clean_reply` 摘除它，页面仍保留完整图文细节、朗读时只挑第一段。
4. **语速调节走整句 `atempo`**：`tts_speed == 1.0` 时逐块直推首包最快；`tts_speed != 1.0` 时需等一句 PCM 收完走 `ffmpeg -filter:a atempo=...` 再推出去，否则会断音。
5. **MiMo Key 和压缩模型配置**：`server.py` 优先读 `MIMO_API_KEY` 环境变量或 `~/.config/mimo/api.key`；`compress_with_gateway()` 默认读 `~/.pi/agent/models.json` 的 `local-gateway`，如果用户用的是环境变量或别的 JSON/中转站，按用户实际文件位置改写那几行读取逻辑即可。

## 6. 落地顺序与验收

1. 确认用户本地 SenseVoice 听写入口和 `~/bin/gbc`（或 tmux 包装）已经就绪。
2. 复制 4 个参考脚本到 `~/voice-link/` 和 `~/.local/bin/语音`（`chmod +x`），按用户机器路径调整 `server.py` 里的 `ASR_SCRIPT`、工作目录 `workdir` 与压缩模型配置。
3. 请用户输入 MiMo API Key 并存到 `~/.config/mimo/api.key`（`chmod 600`）。
4. 语法检查：`python3 -m py_compile ~/voice-link/server.py && zsh -n ~/.local/bin/语音`。
5. 启动并接口验收（不靠真嗓子也能测全链路）：
   - 运行 `~/.local/bin/语音`（确认服务起在 `127.0.0.1:8780`，`语音 状态` 正常）；
   - 用系统 TTS 生成一条测试语音并转 16k 单声道 WAV：
     `say -v Tingting -o /tmp/t.aiff "你好，一加一等于几" && ffmpeg -y -i /tmp/t.aiff -ar 16000 -ac 1 /tmp/t.wav`
   - 调接口验证 SSE 事件顺序：
     `curl -sN -F file=@/tmp/t.wav -F agent=pi http://127.0.0.1:8780/api/turn`
   - 确认依次看到 `heard` → `status` → `say(收到)` → `reply` → `say` → `audio`… → `done`，清理 `/tmp/t.aiff`、`/tmp/t.wav`。
6. 请用户在浏览器页面（`http://127.0.0.1:8780`）亲自点按麦克风授权并说一句话，确认实机听筒与喇叭畅通。
