"""Optional DeepSeek assistant for a team run.

The assistant receives one question that the owner wrote for it, nothing else. The question is
checked on this Mac before it is sent; a hit blocks the whole send and names only the line and
the kind of material, never the material itself. The Harness runs in an empty, temporary profile so
the desktop profile's instructions, memory and sessions stay out; only the credential file is shared.
"""

import os
import re
import tempfile
from pathlib import Path

from . import harness


ASSISTANT_PATCH = Path(__file__).resolve().parent.parent / "team-assistant.patch.yml"
MAX_QUESTION_CHARS = 5000
ASSISTANT_INSTRUCTIONS = (
    "你是大肥鱼，在 MCP Dock 团队运行里担任可选助手。下面是主人核准后单独发给你的一个问题。"
    "只根据这个问题给出独立意见，供复核者参考。不要读写文件，不要索取更多材料；不确定的地方直接说明。"
)

_PRIVATE_KEY = re.compile(r"-----BEGIN [^\r\n]*PRIVATE KEY-----")
_CREDENTIAL_CN = re.compile(r"(?:密码|口令|密钥|私钥|令牌|验证码|账号|帐号)\s*[：:=]")
_LOCAL_PATH = re.compile(
    r"(?:^|[\s\"'(（=:：,，])(?:~|/Users|/home|/Volumes|/private|/var/folders|/root)(?:/|$)"
    r"|(?<![A-Za-z])[A-Za-z]:\\"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.]+)?(?![\w:])")
_NETWORK = re.compile(
    r"(?i)(?<![a-z])(?:clash|vps|dns|v2ray|xray|shadowsocks|wireguard|surge|quantumult|fake-?ip|proxy|socks5?)(?![a-z])"
    r"|(?<![a-z])(?:ip\s+(?:route|addr|address|link|rule|neigh)|ifconfig|traceroute|nslookup|netstat|scutil)(?![a-z])"
    r"|代理|翻墙|梯子|机场节点"
)


def _is_ipv6(candidate):
    return "::" in candidate or candidate.count(":") >= 5


def inspect_question(text):
    """Return one finding per line and kind of private material; the findings never quote it."""
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        kinds = []
        if (_PRIVATE_KEY.search(line) or _CREDENTIAL_CN.search(line)
                or harness._CREDENTIAL_FIELD.search(line) or harness._CREDENTIAL_VALUE.search(line)):
            kinds.append("凭据或密钥")
        if _LOCAL_PATH.search(line):
            kinds.append("本机路径")
        if _IPV4.search(line) or any(_is_ipv6(m.group()) for m in _IPV6_CANDIDATE.finditer(line)):
            kinds.append("IP 地址")
        if _NETWORK.search(line):
            kinds.append("个人网络或代理资料")
        findings.extend(f"第 {number} 行疑似{kind}" for kind in kinds)
    return findings


def _checked(question):
    if (not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS
            or "\x00" in question or any(0xD800 <= ord(ch) <= 0xDFFF for ch in question)):
        raise ValueError(f"给助手的问题须为 1 到 {MAX_QUESTION_CHARS} 字的有效文本")
    hits = inspect_question(question)
    if hits:
        raise ValueError("发送前被拦截，尚未发送：" + "、".join(hits))
    return question


def consult_assistant(question, on_event=None, *, timeout_seconds=None, cancel=None):
    """Send one checked question to DeepSeek from an empty directory and an empty profile.

    Waits without a time limit once DeepSeek confirms receipt; `cancel` stops the wait.
    """
    question = _checked(question)
    source_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    with tempfile.TemporaryDirectory(prefix="deepseek-assistant-") as cwd, \
            tempfile.TemporaryDirectory(prefix="deepseek-assistant-profile-") as profile:
        environment = {"HOME": profile, "DSH_HOME": profile,
                       "MCP_DOCK_CREDENTIAL_FILE": str(source_home / ".credentials.yaml")}
        return harness._run(ASSISTANT_INSTRUCTIONS + "\n\n" + question, cwd, mode="read-only",
                            on_event=on_event, timeout_seconds=timeout_seconds, cancel=cancel,
                            extra_patch=ASSISTANT_PATCH, environment=environment)
