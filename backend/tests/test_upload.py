"""图片上传。

重点不是「能存文件」，而是**上传口子不能被当成任意文件写入的跳板**：
  - 类型按文件头魔数判定，不信任 Content-Type
  - 光有魔数还不够：还要过结构校验，否则「PNG 头 + 任意内容」会被永久存下
  - 文件名自己生成，客户端文件名不参与（否则可路径穿越）
  - 限大小、上传需管理员、读取放开（商品封面要能未登录访问）
  - 读取路径不能穿越出上传目录
"""
from pathlib import Path

from app.core.config import settings

# ---- 结构完整的测试图片（必须能过 _structurally_ok）----
_IHDR_DATA = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + (13).to_bytes(4, "big") + b"IHDR" + _IHDR_DATA + b"\x00\x00\x00\x00"
    + (0).to_bytes(4, "big") + b"IEND" + b"\xaeB\x60\x82"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"  # 必须以 EOI 结束
GIF = b"GIF89a" + b"\x00" * 32 + b"\x3b"  # 必须以 trailer 结束
WEBP = b"RIFF" + (24).to_bytes(4, "little") + b"WEBP" + b"\x00" * 16


def _post(client, content, filename="a.png", ctype="image/png"):
    return client.post(
        "/api/v1/uploads", files={"file": (filename, content, ctype)}
    )


# ---------------- 正常路径 ----------------


def test_admin_can_upload_png(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, PNG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"].startswith(settings.UPLOAD_URL_PREFIX)
    assert body["url"].endswith(".png")
    assert body["size"] == len(PNG)
    name = body["url"].rsplit("/", 1)[-1]
    assert (tmp_path / name).read_bytes() == PNG


def test_upload_detects_type_by_magic(client, tmp_path, monkeypatch):
    """同一份内容换个 Content-Type / 名字，扩展名仍由魔数决定。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    for content, ext in ((JPEG, ".jpg"), (WEBP, ".webp"), (GIF, ".gif")):
        r = _post(client, content, filename="whatever.png", ctype="image/png")
        assert r.status_code == 201, r.text
        assert r.json()["url"].endswith(ext), f"{ext} 应按文件头识别"


# ---------------- 类型校验 ----------------


def test_upload_rejects_non_image_with_image_content_type(client, tmp_path, monkeypatch):
    """把 HTML 标成 image/png 也必须被拒——魔数不匹配。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, b"<script>alert(1)</script>", filename="x.png", ctype="image/png")
    assert r.status_code == 400
    assert "文件头" in r.json()["detail"]


def test_upload_rejects_header_only_image(client, tmp_path, monkeypatch):
    """只有图片头、结构不完整也要拒：否则等于开放「任意内容永久存储」。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert r.status_code == 400
    assert "结构" in r.json()["detail"]


def test_upload_rejects_svg(client, tmp_path, monkeypatch):
    """SVG 能内嵌脚本，是同域 XSS 载体，必须拒（且要有回归保护）。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
              filename="x.svg", ctype="image/svg+xml")
    assert r.status_code == 400


# ---------------- 大小边界 ----------------


def test_upload_size_boundary(client, tmp_path, monkeypatch):
    """恰好等于上限放行，超 1 字节即拒——只测「远超」等于没测边界。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", len(PNG))
    assert _post(client, PNG).status_code == 201

    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", len(PNG) - 1)
    assert _post(client, PNG).status_code == 413


def test_upload_rejects_empty_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, b"")
    assert r.status_code == 400
    assert "为空" in r.json()["detail"]


def test_upload_rejects_oversize(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "UPLOAD_MAX_BYTES", 10)
    assert _post(client, PNG).status_code == 413


# ---------------- 文件名与越权 ----------------


def test_upload_ignores_client_filename(client, tmp_path, monkeypatch):
    """客户端文件名不参与落盘：路径穿越类文件名也不会写到目录外。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    r = _post(client, PNG, filename="../../../evil.png")
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    assert "evil" not in url and ".." not in url
    name = url.rsplit("/", 1)[-1]
    assert (tmp_path / name).exists()


def test_buyer_cannot_upload(buyer_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    assert _post(buyer_client, PNG).status_code == 403


def test_anonymous_cannot_upload(raw_client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    assert _post(raw_client, PNG).status_code == 401


# ---------------- 读取侧 ----------------


def test_uploaded_image_readable_without_auth(client, raw_client):
    """读取不鉴权：商品封面需要未登录（游客）也能看到，且带 nosniff。

    这条刻意用真实的 UPLOAD_DIR（静态挂载指向它），所以跑完要清理。
    """
    r = _post(client, PNG)
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    try:
        got = raw_client.get(url)  # 未带任何凭据
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("image/")
        assert got.headers["x-content-type-options"] == "nosniff"
    finally:
        name = url.rsplit("/", 1)[-1]
        (Path(settings.UPLOAD_DIR) / name).unlink(missing_ok=True)


def test_static_mount_blocks_traversal(raw_client):
    """上传目录的只读挂载不能穿越到仓库其它文件（含编码后的 ..）。"""
    for path in ("/uploads/../README.md", "/uploads/%2e%2e%2f%2e%2e%2fREADME.md"):
        assert raw_client.get(path).status_code == 404, path
