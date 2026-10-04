"""全局配置：所有可变参数集中在此，支持 .env 覆盖。"""
import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """应用配置。默认值可直接跑，生产环境用 .env 覆盖。"""

    PROJECT_NAME: str = "flowmart"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # JWT 签名密钥：生产必须改成强随机值并通过环境变量注入，默认仅开发可用
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    # 短期访问令牌：过期靠 /auth/refresh 静默续期，故设短（默认 30 分钟）。生产可调。
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # 初始管理员用户名：首次 seed / 迁移（init_db、seed）时把该用户提升为管理员。
    # 生产务必改为真实管理员账号，切勿沿用演示值 zhangsan。
    BOOTSTRAP_ADMIN: str = "zhangsan"

    # 跨域（CORS）：仅放行已知前端源，禁止随意 `*`（配合 credentials 使用时 `*` 会被浏览器拒绝）
    CORS_ORIGINS: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]

    # JWT 写入 httpOnly Cookie（防御 XSS 窃令牌，前端不再用 localStorage 存明文令牌）
    JWT_COOKIE_NAME: str = "fm_token"
    COOKIE_SECURE: bool = False  # 生产必须 True（仅 HTTPS 下浏览器才接受 httpOnly+Secure）
    COOKIE_SAMESITE: str = "lax"  # lax / strict：lax 兼容同站前端，strict 抗 CSRF 更强

    # 刷新令牌：长期有效、写独立 httpOnly Cookie，仅用于 /auth/refresh 换发访问令牌
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    REFRESH_TOKEN_COOKIE_NAME: str = "fm_refresh"

    # 登录限流（防暴力破解）：同一 (客户端IP, 用户名) 在窗口内失败超阈值即拒。
    # 内存级固定窗口——单实例足够、零依赖；多实例/生产需换 Redis 等共享存储，
    # 否则限流只对本机请求生效（注释见 backend/app/core/ratelimit.py）。
    LOGIN_RATE_LIMIT_MAX: int = 5
    LOGIN_RATE_LIMIT_WINDOW: int = 60  # 秒

    # 登录限流——客户端 IP 来源（安全开关，默认 False）：
    # False（默认）→ 取 request.client.host（直连真实 socket 地址，客户端无法伪造）；
    # True  → 仅当反向代理已用真实客户端 IP 覆写 X-Forwarded-For 时才取其首跳。
    # 切勿在「客户端可自己塞 X-Forwarded-For」的直连场景开 True，否则攻击者可伪造
    # 不同 XFF 让每次请求都生成新限流键、永远累计不到阈值，限流直接失效。
    LOGIN_RATE_LIMIT_TRUST_PROXY: bool = False

    # 登录限流——多实例/负载均衡共享计数（留空=进程内内存，单实例够用）：
    # 非空时启用 Redis 后端（INCR+EXPIRE 原子计数），限流对全部实例统一生效。
    # 例如 "redis://127.0.0.1:6379/0"。连不上会在启动时直接报错（fail-fast）。
    LOGIN_RATE_LIMIT_REDIS_URL: str = ""

    # ---- 通用限流（注册 / 验证码 / 找回密码）：防灌账号、验证码轰炸 ----
    # 与登录限流的区别：登录只记**失败**（防密码爆破），这里记**每次请求**——
    # 这类端点不看成败只看调用量，一次成功调用同样消耗配额，
    # 否则攻击者可用正确参数高频调用把短信/邮件渠道打爆、或刷出一堆账号。
    # 计数后端与登录限流共用（同一个 LOGIN_RATE_LIMIT_REDIS_URL）。
    RATE_LIMIT_REGISTER_MAX: int = 5  # 每 IP 每窗口最多注册次数
    RATE_LIMIT_CODE_MAX: int = 5  # 每 IP 每窗口最多发码次数（验证 / 找回密码）
    # 登录的 **per-IP** 上限，与上面「登录失败限流」互补：
    # 后者按 (IP, 用户名) 只记失败，换用户名就能重置配额；而每次密码校验都要跑
    # PBKDF2（约几十毫秒 CPU），于是「用户名 × 5 次」会变成 CPU 放大 DoS
    # 与凭证填充的通道。这一层不看用户名也不看成败，只看这个 IP 的登录请求量。
    RATE_LIMIT_LOGIN_MAX: int = 30
    # OTP 确认（6 位码）的猜测上限，按 (IP, 用户名) 记**每次请求**：
    # 每条码自身的 OTP_CONFIRM_MAX_ATTEMPTS 会被「重新发码」重置，
    # 稳态猜码速率 = 尝试数/码 × 码数/分，不额外限住就是个可用的爆破通道。
    RATE_LIMIT_OTP_MAX: int = 10
    # 下单 / 结算：按 **user_id**（不是 IP）计每次请求。create_order 会原子扣库存，
    # 刷单能把库存打到 0 —— 业务型 DoS，比打爆 CPU 更难恢复。
    RATE_LIMIT_ORDER_MAX: int = 20
    # 上传按 user_id 计：每次上传都会写盘，刷上传是最直接的磁盘 DoS
    RATE_LIMIT_UPLOAD_MAX: int = 30
    # 支付回调限流：按 **客户端 IP** 计每次请求（回调无用户身份，只能用 IP 维度）。
    # 回调入口在「验签」之前就做限流——验签是 RSA/AES-GCM 这类 CPU 密集操作，
    # 不挡住的话，伪造签名的洪水请求就是现成的 CPU 放大 DoS（每请求一次非对称验签）。
    # 阈值按「单个渠道 IP 的正常重发节奏（15s 阶梯）」取宽，但足以把洪水压到 1 秒几发以内。
    # 高并发大促商户可酌情调大；设为 0 关闭该 scope。
    RATE_LIMIT_PAYMENT_CALLBACK_MAX: int = 60
    RATE_LIMIT_WINDOW: int = 60  # 秒

    # ---- 图片上传（商品封面等）----
    # 本地磁盘 + 静态目录挂载，零外部依赖；换 OSS/S3 只需替换 api/uploads.py
    # 的写入逻辑，对外返回的 URL 形状不变。
    UPLOAD_DIR: str = str(PROJECT_ROOT / "uploads")
    # 前端构建产物目录。容器镜像会把 vite 构建结果放进来，由后端以根路径提供
    # （**同源部署**：没有跨域，Cookie 的 Secure/SameSite 也不会因跨站而失效）。
    # 开发时该目录不存在，前端仍走 vite dev server（5173），互不影响。
    FRONTEND_DIST: str = str(PROJECT_ROOT / "static")
    UPLOAD_URL_PREFIX: str = "/uploads"
    UPLOAD_MAX_BYTES: int = 2 * 1024 * 1024  # 单文件上限 2MB
    # 上传目录的**总量**上限：单文件有上限挡不住「慢慢攒满磁盘」，
    # 而 /uploads 对外公开可读，失控的目录既是成本问题也是暴露面问题。
    UPLOAD_TOTAL_MAX_BYTES: int = 512 * 1024 * 1024  # 512MB
    # 孤儿文件的保留期：刚上传的图还没挂到任何商品上（上传与保存表单是两次请求），
    # 立刻清理会把用户刚传好的封面删掉，所以必须留宽限期。
    UPLOAD_ORPHAN_RETENTION_SECONDS: int = 24 * 3600
    # 允许的图片类型按**文件头魔数**判定而非信任 Content-Type（见 api/uploads.py），
    # 故这里不提供「允许的类型」配置——类型白名单与魔数表是一一对应的，
    # 放开配置项反而容易配出「声称 png 实际放行任意内容」的洞。

    # 邮箱/手机验证码（OTP）：申请→确认两步式，确认后标记对应渠道已验证。
    OTP_LENGTH: int = 6  # 验证码位数（纯数字）
    OTP_TTL_SECONDS: int = 600  # 验证码有效期（10 分钟）
    # 重发限流（基于 DB，天然多实例安全）：同一用户对同一渠道在窗口内最多发 N 次
    OTP_RESEND_WINDOW: int = 60  # 秒
    OTP_MAX_PER_WINDOW: int = 5
    # 单个验证码的确认尝试上限：超过即锁定该码（防对 6 位码暴力枚举）
    OTP_CONFIRM_MAX_ATTEMPTS: int = 5
    # 发送接口是否把验证码直接回传（仅开发便利）。生产必须 False，否则等于把验证码明文交给客户端。
    OTP_DEV_RETURN_CODE: bool = True
    # ---- 通知渠道（验证码投递）：可插拔 + 配置驱动 ----
    # 本项目是**开源**的，不能替使用者选厂商：这里只定义「怎么连出去」，账号全部由
    # 部署者用环境变量填。默认 console（打印到日志），保证 clone 下来**零凭据**就能
    # 跑通注册 / 验证流程——任何人第一次试用都不该被卡在「先去申请短信服务」上。
    #
    # 邮件与短信**各自独立**可配（否则没法同时「邮件走 SMTP、短信走网关」）：
    #   全局默认 OTP_SENDER，单渠道可用 OTP_EMAIL_SENDER / OTP_SMS_SENDER 覆盖。
    OTP_SENDER: str = "console"  # console | smtp | webhook
    OTP_EMAIL_SENDER: str = ""  # 留空跟随 OTP_SENDER；可选 console | smtp
    OTP_SMS_SENDER: str = ""  # 留空跟随 OTP_SENDER；可选 console | webhook
    # 生产（DEBUG=false）允许渠道落到 console 的**唯一**情形：端到端验收。
    # 验收脚本是从库里读验证码的（见 e2e_acceptance.py），并没有真实渠道可配；
    # 除此之外一律保持 False——console 在生产意味着验证码只进日志，用户收不到，
    # 而任何能看日志的人都能完成任意账号的验证。
    OTP_ALLOW_CONSOLE_IN_PROD: bool = False

    # 邮件（SMTP）：用标准库 smtplib 实现，**零新增依赖**，任何 SMTP 服务商都能连。
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587  # 587=STARTTLS，465=SSL
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""  # 发件人；留空回退到 SMTP_USER
    SMTP_STARTTLS: bool = True  # 587 端口用
    SMTP_USE_SSL: bool = False  # 465 端口用（与 STARTTLS 互斥）
    # SMTP 是**同步阻塞**调用：不设超时，上游一抖动就会把请求线程全部挂住直到打满 worker。
    # 外呼必须有超时，这是依赖外部服务的底线。
    SMTP_TIMEOUT: int = 10  # 秒

    # 短信：**通用 HTTP 网关**，刻意不内置任何一家厂商的 SDK。
    # 理由：各家签名算法不同且会变，内置等于替使用者选厂商，并把他们的 SDK 变成
    # 本项目的运行时依赖（与「不为用不上的东西引依赖」冲突）。
    # 改为把网关地址配上即可——它可以是厂商网关，也可以是自建的一小段转发服务。
    # 报文模板用 {target} / {code} / {ttl} 占位符描述，适配任何字段命名。
    SMS_WEBHOOK_URL: str = ""
    SMS_WEBHOOK_METHOD: str = "POST"  # POST | GET
    SMS_WEBHOOK_BODY: str = '{"to":"{target}","text":"您的验证码是 {code}，{ttl} 秒内有效"}'
    # 额外请求头，JSON 对象字符串，如 {"Authorization":"Bearer xxx"}；留空不加。
    # 默认已带 Content-Type: application/json；模板若写成表单（a=1&b=2），
    # 用这里覆盖成 {"Content-Type":"application/x-www-form-urlencoded"}。
    SMS_WEBHOOK_HEADERS: str = ""
    SMS_WEBHOOK_TIMEOUT: int = 10
    SMS_SUCCESS_MIN_STATUS: int = 200  # 视为发送成功的 HTTP 状态码区间
    SMS_SUCCESS_MAX_STATUS: int = 299

    # ---- 域名、部署环境、TLS ----
    # 本项目开源，不替使用者决定部署形态：同源直跑 / 反代后面 / 前后端分域，全靠这几个开关表达。
    # 单一事实来源是 PUBLIC_BASE_URL，配一次域名尽量复用，别让各处各写一遍。
    DEPLOY_ENV: str = "dev"  # dev | staging | production（仅标签，出现在 /health 便于区分跑的是哪套）
    # 站点公网基址，例 https://shop.example.com：
    #   - 支付回调地址（PAYMENT_NOTIFY_BASE_URL）留空时回退到这里，域名只配一次；
    #   - ENFORCE_HTTPS / HSTS 的跳转目标也基于它。
    PUBLIC_BASE_URL: str = ""
    # Cookie 的 domain 属性：留空 = host-only（同源部署，令牌 Cookie 只发给当前 host）；
    # 前后端分处不同子域（如 api.example.com 与 shop.example.com）时必须设 ".example.com"，
    # 否则浏览器不会把令牌 Cookie 随对 api 域的请求带上，登录态在子域间直接失效。
    COOKIE_DOMAIN: str = ""
    # 反向代理信任开关（全局，默认 False）：是否可信 X-Forwarded-For / X-Forwarded-Proto。
    # 仅在「代理已用真实客户端 IP / 真实协议覆写这两个头」时开 True，否则攻击者可伪造 XFF
    # 绕过限流、或骗过 TLS 判定（详见 ratelimit._client_ip 与 main._is_secure 的注释）。
    TRUST_PROXY: bool = False
    # 强制 HTTPS：True 时把非 https 请求 307 跳到 https（基于请求 host 改写 scheme）。
    # 反代后面**必须同时**开 TRUST_PROXY=True，否则代理转发来的 http 请求会被无限重定向。
    ENFORCE_HTTPS: bool = False
    # HSTS：https 响应上加 Strict-Transport-Security（max-age 秒）；0 = 不发。
    HSTS_MAX_AGE: int = 0  # 生产建议 31536000

    # ---- 支付渠道（可插拔，配置驱动）----
    # 三家渠道共用一套 PaymentProvider 接口（见 app/core/payment.py），切换只改这一项：
    #   mock   —— 演示/开发：发起支付即视为成功，不接真实渠道（保持既有行为）
    #   wechat —— 微信支付 v3（Native 扫码）
    #   alipay —— 支付宝（电脑网站支付）
    # 之所以做成配置而非代码分支：同一套镜像要能在「本地联调 / 预发 / 生产」之间切换，
    # 且渠道凭据必须来自环境变量（不能进代码库）。
    PAYMENT_PROVIDER: str = "mock"

    # 渠道回调的公网基址。真实渠道要把「用户付没付钱」回调给我们，必须是渠道能访问到的
    # 公网地址（本地 127.0.0.1 渠道回调不到）。非 mock 时必填，否则启动即报错——
    # 配错它只会在「用户付款后订单迟迟不更新」时才被发现，那时钱已经收了，最难查。
    PAYMENT_NOTIFY_BASE_URL: str = ""  # 例：https://shop.example.com

    # 微信支付 v3（native 扫码）。注意：JSAPI 需要用户的 openid（得先接公众号/小程序授权），
    # 本项目暂无微信身份体系，故先只实现 Native——扫码支付不需要 openid。
    WECHAT_APPID: str = ""
    WECHAT_MCHID: str = ""  # 商户号
    WECHAT_API_V3_KEY: str = ""  # APIv3 密钥（32 字节，回调 resource 解密用）
    WECHAT_MCH_CERT_SERIAL_NO: str = ""  # 商户 API 证书序列号
    WECHAT_PRIVATE_KEY_PATH: str = ""  # 商户 API 私钥 pem（请求签名用）
    WECHAT_PLATFORM_CERT_PATH: str = ""  # 微信支付平台证书 pem（回调验签用）

    # 支付宝：电脑网站支付（alipay.trade.page.pay），前端拿到的就是一个跳转 URL
    ALIPAY_APPID: str = ""
    ALIPAY_PRIVATE_KEY_PATH: str = ""  # 应用私钥 pem（请求签名用）
    ALIPAY_PUBLIC_KEY_PATH: str = ""  # 支付宝公钥 pem（回调验签用，不是应用公钥）
    ALIPAY_SIGN_TYPE: str = "RSA2"
    ALIPAY_GATEWAY: str = "https://openapi.alipay.com/gateway.do"

    # 待付款订单超时时长（分钟）：超过该时长的待付款订单会被 expire_unpaid_orders
    # 脚本自动取消并归还库存，避免库存被永久占用（资源泄漏）。<=0 表示不启用超时。
    ORDER_PAY_TIMEOUT_MINUTES: int = 30

    # MySQL 连接
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = "1234560"
    DB_NAME: str = "flowmart"
    DB_ECHO: bool = False

    # 数据库类型：sqlite（默认，开发期单文件便于查看）或 mysql（生产）
    DB_DIALECT: str = "sqlite"
    # SQLite 文件路径，留空则默认落在项目根目录 flowmart.db
    SQLITE_PATH: str = ""

    # 直接给定完整连接串时优先级最高，用于连特殊环境
    DATABASE_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _require_strong_secret_in_prod(self):
        # 生产（非 debug）下仍用开发默认密钥意味着任何人都能伪造令牌，必须启动即报错
        if not self.DEBUG and self.SECRET_KEY == "dev-only-insecure-secret-change-me":
            raise ValueError("生产环境必须设置强随机 SECRET_KEY，当前仍为开发默认值")
        return self

    @model_validator(mode="after")
    def _require_secure_cookie_in_prod(self):
        # 生产（非 debug）下若 COOKIE_SECURE 仍为 False，HttpOnly Cookie 会经明文 HTTP 传输，
        # 网络中间人可直接窃取会话令牌。必须启动即报错，而不是悄悄裸奔。
        if not self.DEBUG and not self.COOKIE_SECURE:
            raise ValueError("生产环境必须 COOKIE_SECURE=True，否则会话 Cookie 会经明文 HTTP 泄露")
        return self

    @model_validator(mode="after")
    def _require_bootstrap_admin(self):
        # 去掉首尾空白后再判断：裸 " zhangsan " 这类带空格的值若不归一化，
        # 会和真实用户名匹配不上，导致「谁都不是管理员」、系统静默锁死。
        # 配置错误必须在启动时暴露，而不是运行时悄悄变成不可用。
        self.BOOTSTRAP_ADMIN = self.BOOTSTRAP_ADMIN.strip()
        if not self.BOOTSTRAP_ADMIN:
            raise ValueError("BOOTSTRAP_ADMIN 不能为空：必须指定一个初始管理员用户名")
        return self

    @model_validator(mode="after")
    def _require_payment_settings(self):
        """支付渠道配置：非 mock 时凭据必须齐全，启动即报错。

        为什么必须 fail-fast：支付凭据配错（少一个商户号、环境变量名打错）不会在启动时
        暴露，只会在**用户真的去付款**那一步失败——那时用户已经下单、库存已扣，
        问题表现为「付不了款」或更糟的「付了款订单没更新」，是线上最难查的一类故障。
        """
        self.PAYMENT_PROVIDER = self.PAYMENT_PROVIDER.strip().lower()
        if self.PAYMENT_PROVIDER not in ("mock", "wechat", "alipay"):
            raise ValueError(
                f"PAYMENT_PROVIDER 只能是 mock / wechat / alipay，当前为 {self.PAYMENT_PROVIDER!r}"
            )
        if self.PAYMENT_PROVIDER == "mock":
            return self

        # 回调地址是所有真实渠道的公共前提：渠道要通知我们「钱到了没」。
        # 留空时回退到 PUBLIC_BASE_URL——域名只配一次就够了，不必每个渠道各写一遍。
        notify = (self.PAYMENT_NOTIFY_BASE_URL or self.PUBLIC_BASE_URL).strip().rstrip("/")
        if not notify:
            raise ValueError(
                "PAYMENT_PROVIDER 非 mock 时必须配置 PAYMENT_NOTIFY_BASE_URL（或 PUBLIC_BASE_URL）"
                "作为渠道回调的公网基址"
            )
        if not notify.startswith("https://"):
            # 回调地址必须 HTTPS：它是「钱到账」的唯一通知路径，走明文会被中间人伪造
            raise ValueError("支付回调地址必须是 https://（回调是资金通知，不接受明文）")
        # 统一回填，payments.py 直接读 PAYMENT_NOTIFY_BASE_URL
        self.PAYMENT_NOTIFY_BASE_URL = notify

        required = {
            "wechat": {
                "WECHAT_APPID": self.WECHAT_APPID,
                "WECHAT_MCHID": self.WECHAT_MCHID,
                "WECHAT_API_V3_KEY": self.WECHAT_API_V3_KEY,
                "WECHAT_MCH_CERT_SERIAL_NO": self.WECHAT_MCH_CERT_SERIAL_NO,
                "WECHAT_PRIVATE_KEY_PATH": self.WECHAT_PRIVATE_KEY_PATH,
                "WECHAT_PLATFORM_CERT_PATH": self.WECHAT_PLATFORM_CERT_PATH,
            },
            "alipay": {
                "ALIPAY_APPID": self.ALIPAY_APPID,
                "ALIPAY_PRIVATE_KEY_PATH": self.ALIPAY_PRIVATE_KEY_PATH,
                "ALIPAY_PUBLIC_KEY_PATH": self.ALIPAY_PUBLIC_KEY_PATH,
            },
        }[self.PAYMENT_PROVIDER]
        missing = [k for k, v in required.items() if not str(v).strip()]
        if missing:
            raise ValueError(
                f"PAYMENT_PROVIDER={self.PAYMENT_PROVIDER} 但缺少必需配置：{', '.join(missing)}"
            )
        # 密钥文件是否可读留到真正发起支付时再报（见 core/payment.py）：
        # 容器里证书常由卷挂载，启动那一刻可能还没挂上，这里判存在会把「还没挂好」
        # 误报成「配错了」，反而更难排查。
        return self

    @model_validator(mode="after")
    def _require_no_dev_otp_in_prod(self):
        # 生产（非 debug）下若 OTP_DEV_RETURN_CODE 仍为 True，发送验证码接口会把明文 OTP 回传给客户端，
        # 等于没有验证。必须启动即报错，绝不悄悄把验证码交给前端。
        if not self.DEBUG and self.OTP_DEV_RETURN_CODE:
            raise ValueError("生产环境必须 OTP_DEV_RETURN_CODE=False，否则验证码会被明文回传")
        return self

    @model_validator(mode="after")
    def _require_deploy_and_tls_settings(self):
        """域名 / 部署环境 / TLS 的基础校验，配错在启动时报错而不是悄悄裸奔。"""
        self.DEPLOY_ENV = self.DEPLOY_ENV.strip().lower()
        if self.DEPLOY_ENV not in ("dev", "staging", "production"):
            raise ValueError("DEPLOY_ENV 只能是 dev / staging / production")

        self.PUBLIC_BASE_URL = self.PUBLIC_BASE_URL.strip()
        if self.PUBLIC_BASE_URL:
            # 统一在此规整，调用方（支付 / 跳转）不必各自再 rstrip
            self.PUBLIC_BASE_URL = self.PUBLIC_BASE_URL.rstrip("/")
            if not re.match(r"^https://", self.PUBLIC_BASE_URL):
                raise ValueError(
                    "PUBLIC_BASE_URL 必须是 https://（示例 https://shop.example.com）"
                )

        if self.DEPLOY_ENV == "production" and self.DEBUG:
            # production 是一个明确的「线上」语义标记，绝不该和调试模式同时出现——
            # DEBUG 开着意味着堆栈/文档全开、密钥校验全关，用它跑 production 等于把内网直接暴露。
            raise ValueError("DEPLOY_ENV=production 时必须 DEBUG=False")

        if self.ENFORCE_HTTPS:
            # 强制 HTTPS 必须配了公网基址；且只有在能识别「真实协议」时才安全——
            # 反代后不开 TRUST_PROXY，代理转发来的请求 scheme 恒为 http，会无限重定向。
            if not self.PUBLIC_BASE_URL:
                raise ValueError("ENFORCE_HTTPS=True 必须配置 PUBLIC_BASE_URL（https）")
            if not self.TRUST_PROXY:
                raise ValueError(
                    "ENFORCE_HTTPS=True 必须同时 TRUST_PROXY=True，否则反代后的 http 请求会无限重定向"
                )
        if self.HSTS_MAX_AGE < 0:
            raise ValueError("HSTS_MAX_AGE 不能为负")
        return self

    @model_validator(mode="after")
    def _require_notification_settings(self):
        """通知渠道配置：选了某个发送器，它的连接参数就必须齐——启动即报错。

        与支付同一套理由：这类配置错不会在启动时暴露，而是在**用户注册到一半收不到码**
        时才炸，且现象是「接口 200、用户什么也没收到」，最难排查的一类线上问题。
        """
        self.OTP_SENDER = self.OTP_SENDER.strip().lower()
        self.OTP_EMAIL_SENDER = self.OTP_EMAIL_SENDER.strip().lower()
        self.OTP_SMS_SENDER = self.OTP_SMS_SENDER.strip().lower()

        if self.OTP_SENDER not in ("console", "smtp", "webhook"):
            raise ValueError(
                f"OTP_SENDER 只能是 console / smtp / webhook，当前为 {self.OTP_SENDER!r}"
            )
        # 各渠道可选范围不同：smtp 只能发邮件（短信渠道的 target 是号码，没有邮件语义）；
        # webhook 是「把一段报文 POST 给网关」的通用形态，用来发短信。
        for field, value, allowed in (
            ("OTP_EMAIL_SENDER", self.OTP_EMAIL_SENDER, ("console", "smtp")),
            ("OTP_SMS_SENDER", self.OTP_SMS_SENDER, ("console", "webhook")),
        ):
            if value and value not in allowed:
                raise ValueError(
                    f"{field} 只能是 {' / '.join(allowed)} 或留空（跟随 OTP_SENDER），当前为 {value!r}"
                )

        email_kind = self.OTP_EMAIL_SENDER or self.OTP_SENDER
        sms_kind = self.OTP_SMS_SENDER or self.OTP_SENDER
        # 全局值 + 单渠道未覆盖时可能配出「邮件走 webhook / 短信走 smtp」这种无意义组合
        if email_kind == "webhook":
            raise ValueError("邮件渠道不支持 webhook：请显式设置 OTP_EMAIL_SENDER=smtp 或 console")
        if sms_kind == "smtp":
            raise ValueError("短信渠道不支持 smtp：请显式设置 OTP_SMS_SENDER=webhook 或 console")

        if "smtp" in (email_kind, sms_kind):
            if not self.SMTP_HOST.strip():
                raise ValueError("配置了 smtp 发送器但 SMTP_HOST 为空")
            if not (self.SMTP_FROM.strip() or self.SMTP_USER.strip()):
                raise ValueError("配置了 smtp 发送器但 SMTP_FROM / SMTP_USER 均为空（发件人必填）")
            if self.SMTP_USE_SSL and self.SMTP_STARTTLS:
                raise ValueError("SMTP_USE_SSL 与 SMTP_STARTTLS 互斥（465 用 SSL，587 用 STARTTLS）")

        if sms_kind == "webhook":
            if not self.SMS_WEBHOOK_URL.strip():
                raise ValueError("配置了 webhook 短信发送器但 SMS_WEBHOOK_URL 为空")
            if not self.SMS_WEBHOOK_URL.startswith(("http://", "https://")):
                raise ValueError("SMS_WEBHOOK_URL 必须以 http:// 或 https:// 开头")
            self.SMS_WEBHOOK_METHOD = self.SMS_WEBHOOK_METHOD.strip().upper()
            if self.SMS_WEBHOOK_METHOD not in ("POST", "GET"):
                raise ValueError("SMS_WEBHOOK_METHOD 只能是 POST 或 GET")
            if "{code}" not in self.SMS_WEBHOOK_BODY:
                # 少了 {code} 的话接口照样返回 200、用户照样收不到码，只能靠启动拦住
                raise ValueError("SMS_WEBHOOK_BODY 必须包含 {code} 占位符")
            if self.SMS_WEBHOOK_HEADERS.strip():
                try:
                    parsed = json.loads(self.SMS_WEBHOOK_HEADERS)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"SMS_WEBHOOK_HEADERS 不是合法 JSON：{exc}") from exc
                if not isinstance(parsed, dict):
                    # 非对象（数组 / 字符串）会在真正发送时炸在 headers.update 上——
                    # 那是「第一次发短信才失败」，正是这里要提前拦住的
                    raise ValueError("SMS_WEBHOOK_HEADERS 必须是 JSON 对象，例如 {\"Authorization\":\"Bearer xxx\"}")

        # 生产（非 debug）下仍用 console：验证码只打进服务端日志、用户永远收不到，
        # 且任何能看日志的人都能完成任意账号的验证——等同于验证形同虚设。
        if (
            not self.DEBUG
            and not self.OTP_ALLOW_CONSOLE_IN_PROD
            and "console" in (email_kind, sms_kind)
        ):
            raise ValueError(
                "生产环境必须配置真实发送器：OTP_SENDER / OTP_EMAIL_SENDER / OTP_SMS_SENDER "
                "不能有渠道落到 console（验证码只进日志，用户收不到且日志可见即可冒用）；"
                "若确为端到端验收，请显式设置 OTP_ALLOW_CONSOLE_IN_PROD=true"
            )
        return self

    @property
    def database_url(self) -> str:
        """最终连接串，优先级：DATABASE_URL > DB_DIALECT > MySQL 参数拼装。"""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        if self.DB_DIALECT == "sqlite":
            path = Path(self.SQLITE_PATH) if self.SQLITE_PATH else PROJECT_ROOT / "flowmart.db"
            return f"sqlite:///{path.as_posix()}"
        # charset=utf8mb4 保证 emoji 与生僻字不炸
        return (
            f"mysql+pymysql://{self.DB_USER}:{quote(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def server_database_url(self) -> str:
        """不带库名的连接串，用于建库（仅 MySQL 需要）。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{quote(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/?charset=utf8mb4"
        )

    @property
    def dialect(self) -> str:
        """当前数据库方言名，用于区分 MySQL / SQLite 的差异化处理。"""
        return self.database_url.split("://")[0].split("+")[0]


def quote(pwd: str) -> str:
    """密码里的 @ / : / / 等字符必须转义，否则连接串会被截断解析。"""
    from urllib.parse import quote_plus

    return quote_plus(pwd)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
