"""Reads an error log: its error type, stack frames and a fingerprint.

Mirrors com.loggingx.biz.ai.StackTraces in loggingx-java so both sides group errors the same way.
"""

import hashlib
import re
from dataclasses import dataclass

# at com.shop.OrderService.submit(OrderService.java:88), optionally with a module prefix.
_JAVA_FRAME = re.compile(
    r"^\s*at\s+(?:[\w.$-]+/)*([\w$.<>]+)\.([\w$<>-]+)\(([^:()]+?)(?::(\d+))?\)", re.MULTILINE | re.ASCII
)
# File "/app/shop/orders.py", line 12, in submit
_PYTHON_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)', re.ASCII)
# at submit (/app/src/orders.ts:10:5) or at /app/src/orders.js:10:5
_JS_FRAME = re.compile(
    r"^\s*at\s+(?:([\w$.<> \[\]]+?)\s+\()?((?:file://)?[^\s()]+?\.(?:js|mjs|cjs|jsx|ts|tsx|vue)):(\d+)(?::\d+)?\)?\s*$",
    re.MULTILINE | re.ASCII,
)
# \t/app/orders/service.go:42 +0x1d
_GO_FRAME = re.compile(r"^\s*(/[^\s:]+\.go):(\d+)", re.MULTILINE | re.ASCII)
_ERROR_TYPE = re.compile(r"\b((?:[A-Za-z_][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error|Throwable|Panic|Fault))\b", re.ASCII)
_GO_PANIC = re.compile(r"^panic: (.+)$", re.MULTILINE)
_PYTHON_ERROR_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)", re.ASCII)

# Frames of these packages and folders are libraries, not the application.
_LIBRARY_PACKAGES = (
    "java.", "javax.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.", "scala.", "org.springframework.",
    "org.apache.", "io.netty.", "reactor.", "org.hibernate.", "com.zaxxer.", "org.junit.", "jakarta.",
    "tools.jackson.", "com.fasterxml.", "org.mybatis.", "org.eclipse.", "io.undertow.", "com.mysql.",
    "org.postgresql.", "io.micrometer.", "com.google.", "okhttp3.", "feign.", "io.grpc.", "org.slf4j.", "ch.qos.",
)
_LIBRARY_PATHS = (
    "site-packages/", "dist-packages/", "node_modules/", "/usr/lib/", "/usr/local/lib/", "internal/", "node:",
    "/go/pkg/mod/", "/usr/local/go/",
)


@dataclass(frozen=True, slots=True)
class Frame:
    class_name: str | None
    function: str | None
    file: str
    line: int
    library: bool

    def probable_path(self) -> str:
        """The path the file most likely has in a repository: the package as folders for Java and Kotlin."""

        if self.class_name and "/" not in self.file and "\\" not in self.file:
            package, dot, _ = self.class_name.rpartition(".")
            return f"{package.replace('.', '/')}/{self.file}" if dot else self.file
        path = self.file.replace("\\", "/")
        return path.removeprefix("file://")

    def label(self) -> str:
        place = self.file + (f":{self.line}" if self.line > 0 else "")
        if self.class_name:
            return f"{self.class_name}.{self.function}({place})"
        return place if self.function is None else f"{self.function} ({place})"


@dataclass(frozen=True, slots=True)
class ParsedError:
    error_type: str | None
    headline: str
    frames: tuple[Frame, ...]

    def app_frames(self) -> list[Frame]:
        return [frame for frame in self.frames if not frame.library]

    def location(self) -> Frame | None:
        """The frame the error most likely comes from: the first application frame, else the first frame."""

        app = self.app_frames()
        if app:
            return app[0]
        return self.frames[0] if self.frames else None

    def fingerprint(self) -> str:
        """Error type and the method it was raised in, without line numbers; else the normalized headline."""

        location = self.location()
        error_type = self.error_type or ""
        if location is not None:
            if location.class_name:
                where = f"{location.class_name}.{location.function}"
            else:
                where = f"{location.probable_path()}#{location.function}"
            key = f"{error_type}|{where}"
        else:
            key = f"{error_type}|{normalize(self.headline)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()


def parse(text: str | None) -> ParsedError:
    log = text or ""
    headline = log.splitlines()[0].strip() if log else ""
    frames: list[Frame] = []

    for match in _JAVA_FRAME.finditer(log):
        class_name = match.group(1)
        frames.append(
            Frame(class_name, match.group(2), match.group(3).strip(), int(match.group(4) or 0),
                  _is_library_class(class_name))
        )
    python = [
        Frame(None, match.group(3), match.group(1), int(match.group(2)), _is_library_path(match.group(1)))
        for match in _PYTHON_FRAME.finditer(log)
    ]
    # Python prints the innermost call last.
    frames.extend(reversed(python))
    if not frames:
        for match in _JS_FRAME.finditer(log):
            function = match.group(1).strip() if match.group(1) else None
            frames.append(Frame(None, function, match.group(2), int(match.group(3)), _is_library_path(match.group(2))))
        for match in _GO_FRAME.finditer(log):
            frames.append(Frame(None, None, match.group(1), int(match.group(2)), _is_library_path(match.group(1))))
    return ParsedError(_error_type(log), headline, tuple(frames))


def _error_type(log: str) -> str | None:
    # Python names the error on its last line: "ValueError: invalid literal".
    if "Traceback (most recent call last)" in log:
        lines = [line.strip() for line in log.splitlines() if line.strip()]
        for line in reversed(lines):
            match = _PYTHON_ERROR_LINE.search(line)
            if match and not line.startswith("File "):
                return match.group(1)
    if _GO_PANIC.search(log):
        return "panic"
    match = _ERROR_TYPE.search(log)
    return match.group(1) if match else None


def _is_library_class(class_name: str) -> bool:
    return class_name.startswith(_LIBRARY_PACKAGES) or "$$" in class_name or class_name.startswith("$Proxy")


def _is_library_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return any(part in normalized for part in _LIBRARY_PATHS)


def normalize(line: str | None) -> str:
    """The line with what varies between occurrences replaced: quoted values, ids, numbers."""

    value = line or ""
    value = re.sub(r"\"[^\"]*\"|'[^']*'", "*", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "#", value,
                   flags=re.ASCII)
    value = re.sub(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{12,}\b", "#", value, flags=re.ASCII)
    value = re.sub(r"\d+", "#", value, flags=re.ASCII)
    # Leading timestamps and levels differ in format only.
    value = re.sub(r"^[#:\-./ T,+Z\[\]]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:200]
