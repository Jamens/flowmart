"""图片上传（商品封面等）。

安全要点——每一条都对应一种真实攻击，不要简化掉：

1. **不信任客户端文件名**：自己生成随机名，扩展名只由**魔数**推导。
   否则 `../../etc/passwd`（路径穿越）、`a.php.jpg`（被当脚本执行）都可能得手。
2. **按魔数（文件头）判类型，而不是信任 Content-Type**：Content-Type 完全由
   客户端控制，把一段 HTML/脚本标成 `image/png` 就能绕过。
3. **再补一道结构校验**：只看前 12 字节的话，「PNG 头 + 任意内容」会被永久存下
   并对外可读。今天浏览器不会把它 sniff 成 HTML，但那等于把安全性押在
   「下游 Content-Type 永远正确」这个不可验证的假设上。
4. **在接收之前就按 Content-Length 拦一道**：endpoint 里拿到的 file 已经是
   「整个请求体被接收并落到临时文件之后」的结果，所以下面的 read 只能限制
   「最终存多少」，限制不了「服务器先收了多少」。磁盘 DoS 得在这里挡。
5. **原子写 + 显式错误映射**：写一半失败会留下损坏文件，而它会被 /uploads
   正常对外提供——用户看到的是永久坏图，而不是「上次上传失败」。

权限分工：上传是管理员操作（商品写操作已经是 require_admin，封面属同一类）；
而**读取**走静态目录、不鉴权——商品图片需要未登录也能看，这是刻意的。
"""
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.core.config import settings
from app.core.ratelimit import rate_limit_user
from app.core.security import require_admin
from app.models.ecommerce import User

router = APIRouter(prefix="/uploads", tags=["上传"])

# multipart 边界与首部的开销余量，仅用于 Content-Length 的粗判
_MULTIPART_SLACK = 64 * 1024

# 魔数 -> 扩展名。以**文件头**为准，与 Content-Type / 原始文件名无关。
# 刻意不含 SVG：`image/svg+xml` 里能内嵌脚本，是真正的同域 XSS 载体。
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


def _sniff(head: bytes) -> str | None:
    """按文件头判断图片类型，返回扩展名；非白名单类型返回 None。"""
    for sig, ext in _MAGIC:
        if head.startswith(sig):
            return ext
    # WebP："RIFF" + 4 字节长度 + "WEBP"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def _structurally_ok(data: bytes, ext: str) -> bool:
    """极简结构校验：挡掉「图片头 + 任意 trailer」的粗暴拼接。

    不做完整解码（那要引入 Pillow 这类新运行时依赖），只查各格式必需的起止标记。
    挡不住精心构造的图片隐写，但能挡住最粗暴的拼接攻击，成本为零。
    """
    if ext == "png":
        # 首个块必须是 IHDR，且必须以 IEND 块结尾（IEND + 固定 CRC）
        return data[12:16] == b"IHDR" and data.endswith(b"IEND\xaeB\x60\x82")
    if ext == "jpg":
        return data.endswith(b"\xff\xd9")  # EOI
    if ext == "gif":
        return data.endswith(b"\x3b")  # trailer
    if ext == "webp":
        return len(data) >= 16
    return False


def _dir_size(upload_dir: Path) -> int:
    """上传目录当前占用字节数。

    每次上传都扫一遍：配额内（512MB / 单图 2MB ≈ 数百个文件）scandir 的开销
    远小于一次图片写入，可接受。真到万级文件再换成增量计数器。
    """
    total = 0
    with os.scandir(upload_dir) as it:
        for entry in it:
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue  # 并发下文件可能已被清理，跳过即可
    return total


@router.post("", status_code=201, summary="上传图片（仅管理员；返回可访问的 URL）")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(require_admin),
    # 每次上传都要写盘，刷上传是最直接的磁盘 DoS；按 user_id 计（同下单/结算的理由）
    _rl: None = Depends(rate_limit_user("upload")),
):
    # 第一道闸：按声明长度早早拒绝。这是唯一能在「请求体被完整接收并落临时文件」
    # 之前生效的位置——endpoint 里再怎么读，都已经是接收之后的事了。
    # Content-Length 可伪造，但它与下面「实际大小」的校验是互补的两层。
    declared = request.headers.get("content-length")
    # multipart 自带边界与首部开销，阈值留出余量以免误伤合法请求；
    # 这一层只挡「明显超大」的滥用，精确判定交给下面的实际大小校验。
    if (
        declared and declared.isdigit()
        and int(declared) > settings.UPLOAD_MAX_BYTES + _MULTIPART_SLACK
    ):
        raise HTTPException(status_code=413, detail="图片超过上限")

    # 多读 1 字节用于判定超限：限制的是「最终持久化多少」
    data = await file.read(settings.UPLOAD_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > settings.UPLOAD_MAX_BYTES:
        limit_mb = settings.UPLOAD_MAX_BYTES // 1024 / 1024
        raise HTTPException(status_code=413, detail=f"图片超过 {limit_mb:g}MB 上限")

    ext = _sniff(data[:12])
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="仅支持 jpg / png / gif / webp 图片（按文件头校验，不信任 Content-Type）",
        )
    if not _structurally_ok(data, ext):
        raise HTTPException(
            status_code=400, detail=f"文件头是 {ext}，但内容结构不完整，已拒绝"
        )

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 总量上限：单文件上限挡不住「慢慢攒满磁盘」。写入前先算现有占用，
    # 超限直接拒——等写一半才发现满了会留下半截文件，后面的请求也会连环失败。
    if _dir_size(upload_dir) + len(data) > settings.UPLOAD_TOTAL_MAX_BYTES:
        raise HTTPException(status_code=507, detail="上传空间已满，请先清理旧图片")

    # 随机名 + 由魔数推导的扩展名：客户端文件名与 Content-Type 都不参与
    name = f"{secrets.token_hex(16)}.{ext}"
    # 原子写：先写 .part 再 rename，避免写一半失败留下会被 /uploads 正常提供的坏图
    tmp = upload_dir / f".{name}.part"
    try:
        tmp.write_bytes(data)
        tmp.replace(upload_dir / name)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=507, detail="存储空间不足或写入失败")

    return {"url": f"{settings.UPLOAD_URL_PREFIX}/{name}", "size": len(data)}
