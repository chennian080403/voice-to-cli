#!/usr/bin/env python3
"""语音直连：对着网页说话，原话发给本机前台 AI 会话（Pi / Claude Code / Kimi），把回复念出来。

语音只是传话管道，不当大脑：
  麦克风 → 本地 SenseVoice 听写 → 热词纠正 → 语音命令拦截 → gbc 发给前台会话 → MiMo 合成念出来

只监听 127.0.0.1:8780，只用标准库。配置在同目录 config.json，密钥只从文件/环境读，不写日志。
"""
import base64
import email.parser
import email.policy
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

LOG_PATH = HOME / "Library/Logs/voice-link.log"
ASR_SCRIPT = HOME / ".local/bin/sensevoice-asr"
TTS_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_KEY_FILE = HOME / ".config/mimo/api.key"
AGENTS = ("pi", "claude", "kimi")
NAMES = {"pi": "Pi", "claude": "Claude", "kimi": "Kimi"}
TTS_RATE = 24000


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:  # noqa: BLE001 — 日志写不了也不能让会话挂掉
        pass


def _load_asr():
    loader = SourceFileLoader("transcribe_media", str(ASR_SCRIPT.resolve()))
    spec = importlib.util.spec_from_loader("transcribe_media", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ASR = _load_asr()
FFMPEG = ASR._ffmpeg_path()
GBC = Path(os.path.expanduser(CFG.get("gbc", "~/bin/gbc")))
WORKDIR = os.path.expanduser(CFG.get("workdir", "~/Documents"))
SESSIONS = {a: f"gb-voice-{a}" for a in AGENTS}

SPEED = float(CFG.get("tts_speed", 1.0) or 1.0)
VOICE = CFG.get("tts_voice", "冰糖")
PLACEHOLDER = CFG.get("placeholder", "（语音对话：先用一两句口语说结论，别用列表和代码块开头，细节放后面）")

STATE = {"agent": CFG.get("default_agent", "pi") if CFG.get("default_agent") in AGENTS else "pi",
         "ask": ""}
LOCK = threading.Lock()
GENERATION = [0]          # 每来一轮 +1，旧的一轮看到变了就停止推音频
CACHE = {}                # 预合成的短句 pcm
CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 听写

def transcribe(wav_bytes: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="voice-link-asr-") as tmp:
        src = Path(tmp) / "in.wav"
        src.write_bytes(wav_bytes)
        ASR.transcribe(src, Path(tmp) / "out", None, correction=False)
        return (Path(tmp) / "out" / "transcript.txt").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- 热词纠正

def correct_hotwords(text: str) -> str:
    out = text
    for entry in CFG.get("hotwords", []):
        to = entry.get("to", "")
        words = sorted(entry.get("from", []), key=len, reverse=True)
        for w in words:
            if entry.get("scope") == "command":
                # 派 / π 只在「切到派」「用派」这类命令里换，别全局乱换
                pat = re.compile(r"(切到|切换到|换到|换成|改用|转到|用)\s*" + re.escape(w), re.I)
                out = pat.sub(lambda m: m.group(1) + to, out)
            else:
                out = re.sub(re.escape(w), to, out, flags=re.I)
    return out


# ---------------------------------------------------------------- 语音命令

STOP_WORDS = {"停", "停下", "停下来", "停止", "别做了", "别干了", "别做", "打断", "打住", "取消"}
SWITCH_RE = re.compile(r"^(?:切到|切换到|换到|换成|改用|转到|用|接)(Pi|Claude|Kimi)$", re.I)
PICK_RE = re.compile(r"^(?:帮我|请)?(?:选|选第|要第|第|按)?([一二三四五六七八九十两0-9]+)(?:个|项|条|号)?$")
CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def normalize_cmd(text: str) -> str:
    t = re.sub(r"\s+", "", text)
    t = re.sub(r"[，。！？、,.!?~～…\s]+$", "", t)
    t = re.sub(r"(吧|啊|呀|呢|哦|呗|一下|嘛)$", "", t)
    return t


def parse_number(s: str):
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if s.startswith("十") and len(s) == 2 and s[1] in CN_NUM:
        return 10 + CN_NUM[s[1]]
    if len(s) == 2 and s[0] in CN_NUM and s[1] == "十":
        return CN_NUM[s[0]] * 10
    if len(s) == 3 and s[1] == "十":
        return CN_NUM.get(s[0], 0) * 10 + CN_NUM.get(s[2], 0)
    return None


# ---------------------------------------------------------------- gbc

def gbc(agent: str, *args, timeout: float = 660) -> tuple:
    env = dict(os.environ)
    env["GBC_SESSION"] = SESSIONS.get(agent, f"gb-voice-{agent}")
    try:
        p = subprocess.run([str(GBC), f"--{agent}", *args], capture_output=True, text=True,
                           timeout=timeout, cwd=WORKDIR, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "（等太久了，没等到这一轮做完）"
    except Exception as exc:  # noqa: BLE001
        return 1, f"（gbc 调用失败：{exc}）"


def gbc_busy(agent: str) -> str:
    rc, out = gbc(agent, "busy", timeout=15)
    out = out.strip()
    return out if out in ("yes", "no", "ask") else ("no" if rc else out)


def gbc_pending_ask(agent: str) -> str:
    rc, out = gbc(agent, "wait", "4", timeout=20)
    return clean_reply(out) if rc == 3 else ""


# ---------------------------------------------------------------- 回复清洗 / 要念的那段

def clean_reply(raw: str) -> str:
    lines = []
    noise = re.compile(r"^(▶ 发：|（中间用了|已开：会话|（会话 gb-|—— |ℹ️|⚠️|❌|已插话|已发出)")
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            lines.append(ln)
            continue
        if noise.match(s):
            continue
        if re.fullmatch(r"［上下文[^］]*］", s):
            continue
        if s in (PLACEHOLDER, "（语音对话：先用一两句口语说结论，别用列表和代码块开头，细节放后面）"):
            continue
        if s.startswith("（会话") and "开好" in s:
            continue
        lines.append(ln)
    text = "\n".join(lines).strip()
    text = text.replace(PLACEHOLDER, "")
    text = re.sub(r"（语音对话：[^）]*）", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def spoken_text(reply: str) -> str:
    t = reply or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"<?https?://\S+>?", " ", t)
    t = re.sub(r"(?:~|/)(?:[\w.@\-]+/){2,}[\w.@\-]*", " ", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"\*\*|__|[*_~|]", "", t)
    t = re.sub(r"［上下文[^］]*］", " ", t)

    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    para = paras[0] if paras else t.strip()
    para = re.sub(r"\s+", " ", para).strip()
    if not para:
        return ""
    if len(para) > 220:
        short = compress_with_gateway(para)
        if short:
            return short
        para = para[:220]
    if len(para) > 150:
        head = para[:150]
        cuts = list(re.finditer(r"[。！？!?]", head))
        para = head[:cuts[-1].end()] if cuts else head
    return para


def compress_with_gateway(text: str) -> str:
    cfg = CFG.get("gateway") or {}
    if not cfg:
        return ""
    try:
        models = json.loads(Path(os.path.expanduser(cfg.get("models_json", "~/.pi/agent/models.json")))
                            .read_text(encoding="utf-8"))
        prov = models.get("providers", {}).get(cfg.get("provider", "local-gateway"), {})
        base = prov.get("baseUrl", "http://127.0.0.1:3000/v1").rstrip("/")
        key = prov.get("apiKey", "")
        if not key:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    body = {
        "model": cfg.get("model", "deepseek-v4.1-flash"),
        "messages": [
            {"role": "system", "content": "把这段 AI 回复压成两三句口语，直接说结论，"
                                          "不要列表、不要 markdown、不要代码，80 字以内。"},
            {"role": "user", "content": text[:4000]},
        ],
        "temperature": 0.3,
        "reasoning_effort": "none",
    }
    data = None
    for attempt in (0, 1):
        b = dict(body)
        if attempt:
            b.pop("reasoning_effort", None)   # 网关不认这个参数就去掉
        try:
            req = urllib.request.Request(base + "/chat/completions",
                                         data=json.dumps(b, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + key},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            data = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 422):
                continue
            return ""
        except Exception:  # noqa: BLE001
            return ""
    data = re.sub(r"\s+", " ", (data or "")).strip()
    return data[:220]


# ---------------------------------------------------------------- 语音合成

def _mimo_key() -> str:
    key = os.environ.get("MIMO_API_KEY", "").strip()
    if key:
        return key
    try:
        return MIMO_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def tts_pcm_stream(text: str):
    """逐块产出 pcm16 24kHz 单声道字节。"""
    key = _mimo_key()
    if not key:
        log("合成失败：没有 MiMo key")
        return
    body = {
        "model": "mimo-v2.5-tts",
        "messages": [{"role": "assistant", "content": text}],
        "audio": {"format": "pcm16", "voice": VOICE},
        "stream": True,
    }
    req = urllib.request.Request(TTS_URL, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    d = json.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
                choices = d.get("choices") or []
                if not choices:
                    continue
                aud = ((choices[0].get("delta") or {}).get("audio") or {}).get("data")
                if aud:
                    yield base64.b64decode(aud)
    except Exception as exc:  # noqa: BLE001
        log(f"合成出错：{exc}")


def _atempo(pcm: bytes, speed: float) -> bytes:
    cmd = [FFMPEG, "-loglevel", "error", "-y",
           "-f", "s16le", "-ar", str(TTS_RATE), "-ac", "1", "-i", "pipe:0",
           "-filter:a", f"atempo={speed}",
           "-f", "s16le", "-ar", str(TTS_RATE), "-ac", "1", "pipe:1"]
    try:
        p = subprocess.run(cmd, input=pcm, capture_output=True, timeout=120)
        return p.stdout if p.returncode == 0 and p.stdout else pcm
    except Exception:  # noqa: BLE001
        return pcm


def split_sentences(text: str) -> list:
    parts = re.split(r"(?<=[。！？!?\n])", text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and len(p) < 6:
            out[-1] += p
        else:
            out.append(p)
    return out


def synthesize_sentences(text: str):
    """按句产出 pcm；speed ≠ 1 时整句收完再用 atempo 加速。"""
    for sent in split_sentences(text):
        if SPEED == 1.0:
            yield from tts_pcm_stream(sent)
        else:
            buf = b"".join(tts_pcm_stream(sent))
            if buf:
                yield _atempo(buf, SPEED)


def cached_pcm(text: str) -> bytes:
    with CACHE_LOCK:
        return CACHE.get(text, b"")


def warm_cache(phrases) -> None:
    for ph in phrases:
        try:
            pcm = b"".join(tts_pcm_stream(ph))
            if pcm:
                with CACHE_LOCK:
                    CACHE[ph] = pcm
                log(f"预合成好「{ph}」{len(pcm)}B")
        except Exception as exc:  # noqa: BLE001
            log(f"预合成「{ph}」失败：{exc}")


# ---------------------------------------------------------------- HTTP

def _parse_multipart(content_type: str, body: bytes) -> dict:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = part.get_payload(decode=True)
    return fields


class StaleTurn(Exception):
    """页面又发了一轮新的，这一轮别再往外推了。"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _gen = None

    # ---- 基础
    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            agent = STATE["agent"]
            ask = STATE["ask"] or ""
            busy = "no"
            try:
                busy = gbc_busy(agent)
            except Exception:  # noqa: BLE001
                pass
            if busy == "ask" and not ask:
                ask = gbc_pending_ask(agent)
            return self._json(200, {"agent": agent, "agents": list(AGENTS),
                                    "names": NAMES, "busy": busy, "ask": ask,
                                    "tts_speed": SPEED})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/turn":
            return self._turn()
        return self._json(404, {"error": "not found"})

    # ---- SSE 帮助
    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _emit(self, obj: dict) -> None:
        if self._gen is not None and GENERATION[0] != self._gen:
            raise StaleTurn()          # 来了一轮新的，旧的一轮停手
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _end_sse(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ---- 主流程
    def _turn(self):
        length = int(self.headers.get("Content-Length") or 0)
        fields = _parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
        audio = fields.get("file")
        agent = (fields.get("agent") or b"").decode("utf-8", "ignore").strip()
        if agent not in AGENTS:
            agent = STATE["agent"]

        with LOCK:
            GENERATION[0] += 1
            my_gen = GENERATION[0]
        self._gen = my_gen
        STATE["agent"] = agent

        t_turn = time.perf_counter()
        timing = {}
        self._start_sse()
        try:
            if not audio:
                self._emit({"type": "reply", "text": "（没收到声音，再按一次说一遍？）"})
                self._emit({"type": "done", "timing": {"total": 0}})
                return self._end_sse()

            # 1) 听写
            t = time.perf_counter()
            try:
                raw_text = transcribe(audio)
            except Exception as exc:  # noqa: BLE001
                log(f"听写失败：{exc}")
                self._emit({"type": "reply", "text": "（听写失败了，再说一遍？）"})
                self._emit({"type": "done", "timing": {"total": int((time.perf_counter() - t_turn) * 1000)}})
                return self._end_sse()
            timing["听写"] = int((time.perf_counter() - t) * 1000)

            text = correct_hotwords(raw_text)
            self._emit({"type": "heard", "text": text})
            if not text:
                self._emit({"type": "reply", "text": "（没听清，再说一遍？）"})
                self._emit({"type": "done", "timing": {"total": int((time.perf_counter() - t_turn) * 1000)}})
                return self._end_sse()

            cmd = normalize_cmd(text)

            # 2) 语音命令：打断
            if cmd in STOP_WORDS:
                gbc(agent, "esc", timeout=15)
                STATE["ask"] = ""
                return self._speak_reply("好，已打断", timing, t_turn, spoken="好，已打断")

            # 3) 语音命令：切对象
            m = SWITCH_RE.match(cmd)
            if m:
                target = m.group(1).lower()
                target = {"claude": "claude", "pi": "pi", "kimi": "kimi"}[target]
                STATE["agent"] = target
                msg = f"已切到 {NAMES[target]}"
                return self._speak_reply(msg, timing, t_turn, spoken=msg)

            # 4) 语音命令：选确认框
            pm = PICK_RE.match(cmd)
            if pm:
                num = parse_number(pm.group(1))
                pending = STATE["ask"]
                if pending or gbc_busy(agent) == "ask":
                    if num:
                        STATE["ask"] = ""
                        t = time.perf_counter()
                        # Pi 的选择框没有编号，只能方向键挪到第 n 项再回车；Claude / Kimi 直接按数字
                        keys = (["down"] * (num - 1) + ["enter"]) if agent == "pi" else [str(num)]
                        rc, out = gbc(agent, "key", *keys, timeout=600)
                        timing["选完等回复"] = int((time.perf_counter() - t) * 1000)
                        if rc == 3:
                            ask = clean_reply(out) or "（还在等你选）"
                            STATE["ask"] = ask
                            self._emit({"type": "reply", "text": ask})
                            self._emit({"type": "ask", "text": ask})
                            return self._speak_audio_from(self._ask_spoken(ask), timing, t_turn)
                        reply = clean_reply(out) or "（没拿到回复）"
                        return self._speak_reply(reply, timing, t_turn)

            # 5) 原话发给 AI；同时立刻念「收到」
            self._emit({"type": "status", "text": f"已发给 {NAMES[agent]}，正在想…"})
            t_ai = time.perf_counter()
            box = {}

            def worker():
                box["rc"], box["out"] = gbc(agent, "send", f"{text}\n{PLACEHOLDER}", "600", timeout=660)

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            self._play_cached_or_synth("收到", timing, t_turn)

            th.join(timeout=680)
            if "rc" not in box:
                box["rc"], box["out"] = 124, "（等太久了，没等到这一轮做完）"
            timing["发给AI"] = int((time.perf_counter() - t_ai) * 1000)

            out = box["out"]
            if box["rc"] == 3:
                ask = clean_reply(out) or "（有个确认框要你选）"
                STATE["ask"] = ask
                self._emit({"type": "reply", "text": ask})
                self._emit({"type": "ask", "text": ask})
                return self._speak_audio_from(self._ask_spoken(ask), timing, t_turn)

            reply = clean_reply(out)
            if not reply:
                reply = "（这轮没拿到回复，可能它还在做）"
            self._emit({"type": "reply", "text": reply})
            return self._speak_reply(reply, timing, t_turn)
        except StaleTurn:
            pass                        # 新的一轮接手了，这一轮安静收场
        except (BrokenPipeError, ConnectionResetError):
            with LOCK:
                pass  # 页面走了或来了一轮新的，安静收场
        except Exception as exc:  # noqa: BLE001
            log(f"这一轮出错：{exc}")
            try:
                self._emit({"type": "reply", "text": f"（出错了：{exc}）"})
                self._emit({"type": "done", "timing": timing})
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                self._end_sse()
            except Exception:  # noqa: BLE001
                pass

    # ---- 念
    def _ask_spoken(self, ask: str) -> str:
        t = clean_reply(ask)
        t = re.sub(r"^.*?在等你选：", "", t)
        t = re.sub(r"^(要批准|要选)：", "", t)
        return t.strip()[:220] or "有个确认框要你选"

    def _play_cached_or_synth(self, text: str, timing: dict, t_turn: float) -> None:
        pcm = cached_pcm(text)
        if pcm:
            self._emit({"type": "say", "text": text})
            for i in range(0, len(pcm), 48000):
                self._audio_chunk(pcm[i:i + 48000], timing, t_turn)
            return
        self._speak_audio_from(text, timing, t_turn)

    def _audio_chunk(self, pcm: bytes, timing: dict, t_turn: float) -> None:
        if not pcm:
            return
        if "首个音频" not in timing:
            timing["首个音频"] = int((time.perf_counter() - t_turn) * 1000)
        self._emit({"type": "audio", "pcm": base64.b64encode(pcm).decode()})

    def _speak_audio_from(self, text: str, timing: dict, t_turn: float) -> None:
        text = (text or "").strip()
        if not text:
            return self._emit({"type": "done", "timing": timing})
        self._emit({"type": "say", "text": text})
        try:
            pcm = cached_pcm(text)
            if pcm:
                for i in range(0, len(pcm), 48000):
                    self._audio_chunk(pcm[i:i + 48000], timing, t_turn)
            else:
                for chunk in synthesize_sentences(text):
                    self._audio_chunk(chunk, timing, t_turn)
        except (StaleTurn, BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"念出错：{exc}")
        self._log_turn(timing, t_turn)
        self._emit({"type": "done", "timing": timing})

    def _speak_reply(self, reply: str, timing: dict, t_turn: float, spoken: str = "") -> None:
        if not spoken:
            t = time.perf_counter()
            spoken = spoken_text(reply)
            timing["压缩"] = int((time.perf_counter() - t) * 1000)
        if not spoken:
            self._log_turn(timing, t_turn)
            return self._emit({"type": "done", "timing": timing})
        return self._speak_audio_from(spoken, timing, t_turn)

    def _log_turn(self, timing: dict, t_turn: float) -> None:
        total = int((time.perf_counter() - t_turn) * 1000)
        parts = " ".join(f"{k}={v}ms" for k, v in timing.items())
        log(f"[一轮] agent={STATE['agent']} {parts} 总计={total}ms")


def warm_asr() -> None:
    """启动时先转一段静音，把 SenseVoice 模型页载入内存，第一轮听写就不用等冷启动。"""
    try:
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 8000)
        t = time.perf_counter()
        text = transcribe(bio.getvalue())
        log(f"听写预热完成 {int((time.perf_counter()-t)*1000)}ms（{text!r}）")
    except Exception as exc:  # noqa: BLE001
        log(f"听写预热失败：{exc}")


def main() -> int:
    host = CFG.get("host", "127.0.0.1")
    port = int(CFG.get("port", 8780))
    log(f"启动：http://{host}:{port}/ 速度 x{SPEED}")
    threading.Thread(target=warm_asr, daemon=True).start()
    threading.Thread(target=warm_cache, args=(["收到", "好，已打断"],), daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
