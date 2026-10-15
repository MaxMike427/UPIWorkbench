"""1.2.0 服务商目录。

来源：doc/供应商.md（同时保留 doc/服务商.md 中的在用条目）。
只记录公开入口，不包含 CDK、账号或密钥。
"""
from __future__ import annotations

PROVIDERS = [
    # 账号库
    {"id": "account_0217", "name": "账号供应商 · 0217", "category": "account", "provider_type": "account",
     "url": "https://wzyp.cn/shop/CPZF8PBK", "buy_url": "https://wzyp.cn/shop/CPZF8PBK",
     "cdk_key": "", "integrated": False, "combined": False},
    {"id": "account_ai_wholesale", "name": "账号供应商 · AI 批发商", "category": "account", "provider_type": "account",
     "url": "https://wzyp.cn/shop/8UOIIRW2", "buy_url": "https://wzyp.cn/shop/8UOIIRW2",
     "cdk_key": "", "integrated": False, "combined": False},
    # 接码
    {"id": "handy", "name": "Handy 融合接码", "category": "sms", "provider_type": "sms",
     "url": "https://apihub.space/handy/", "buy_url": "https://wzyp.cn/shop/8Q9RHH0W",
     "cdk_key": "sms_provider.cdk", "integrated": True, "combined": False},
    {"id": "iruozhen", "name": "若真 AI·codex 短效实卡", "category": "sms", "provider_type": "sms",
     "url": "https://sms.iruozhen.com/", "buy_url": "https://www.16688.com.cn/goods/G96053095",
     "cdk_key": "sms_provider.cdk", "integrated": False, "combined": False},
    {"id": "hero_sms", "name": "HeroSMS", "category": "sms", "provider_type": "sms",
     "url": "https://hero-sms.com/", "buy_url": "https://hero-sms.com/",
     "cdk_key": "sms_provider.cdk", "integrated": False, "combined": False},
    # 提炼
    {"id": "lumen", "name": "流光 Lumen Flow", "category": "extract", "provider_type": "extract",
     "url": "https://int31.space/public/checkout", "buy_url": "https://wzyp.cn/shop/lumen-ai",
     "cdk_key": "extract_provider.cdk", "integrated": False, "combined": False},
    {"id": "team_free_extract", "name": "team 免费提炼", "category": "extract", "provider_type": "extract",
     "url": "https://qitucloud.l.cd/team/", "buy_url": "https://qitucloud.l.cd/team/",
     "cdk_key": "extract_provider.cdk", "integrated": False, "combined": False},
    {"id": "local", "name": "本地提炼（upi-zero-link）", "category": "extract", "provider_type": "extract",
     "url": "https://github.com/wangshen233/upi-zero-link", "buy_url": "https://github.com/wangshen233/upi-zero-link",
     "cdk_key": "", "integrated": True, "combined": False},
    # 提炼 + 支付
    {"id": "ytc", "name": "ytc 提炼加支付（暗光提链）", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://ytc.xxsyun.xyz/", "buy_url": "https://wzyp.cn/shop/Camellia",
     "cdk_key": "payment_provider.cdk", "integrated": True, "combined": True},
    {"id": "xxsyun_upi", "name": "UPI 雇主中心", "category": "pay", "provider_type": "pay",
     "url": "https://upi.xxsyun.xyz/", "buy_url": "https://upi.xxsyun.xyz/employer/tickets/",
     "cdk_key": "payment_provider.cdk", "integrated": True, "combined": True},
    {"id": "seashore", "name": "Seashore 扫码站", "category": "pay", "provider_type": "pay",
     "url": "https://seashore.lol/publisher", "buy_url": "https://seashore.lol/publisher",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": False},
    {"id": "masi", "name": "皮蛋扫码", "category": "pay", "provider_type": "pay",
     "url": "https://masi.cc.cd/extract", "buy_url": "https://wzyp.cn/shop/L8JH75L1",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": False},
    {"id": "wangyou", "name": "春晴提链", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://wangyou.dric52.com/", "buy_url": "https://wangyou.dric52.com/",
      "cdk_key": "payment_provider.cdk", "integrated": True, "combined": True},
    {"id": "layo", "name": "Layo 提链工作台", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://layo.club/", "buy_url": "https://layo.club/", "cdk_key": "payment_provider.cdk",
     "integrated": False, "combined": True, "note": "只支持 HTTP/HTTPS 代理"},
    {"id": "ai888", "name": "AI888 Quick Link", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://ticdk.linlinflow.ccwu.cc/ticdk/", "buy_url": "https://pay.ldxp.cn/item/mjocfy",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": True},
    {"id": "timeline", "name": "风火轮聚合提炼", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://paylink.timeline.kdns.fr/", "buy_url": "https://catfk.com/shop/4JH37BDS",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": True},
    {"id": "xql", "name": "XQL 提链", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://link.xqlbest.com/", "buy_url": "https://link.xqlbest.com/",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": True},
    {"id": "xiaoliai", "name": "冲云链", "category": "extract_pay", "provider_type": "extract_pay",
     "url": "https://upi.xiaoliai.shop/", "buy_url": "https://wzyp.cn/item/zp7f46",
     "cdk_key": "payment_provider.cdk", "integrated": False, "combined": True},
]


def providers_for(capability: str):
    if capability in ("account", "sms", "extract", "pay", "extract_pay"):
        if capability == "account":
            return [p for p in PROVIDERS if p["category"] == "account" or p["provider_type"] == "account"]
        if capability == "extract_pay":
            return [p for p in PROVIDERS if p["category"] == "extract_pay" or p["provider_type"] == "extract_pay"]
        if capability == "sms":
            return [p for p in PROVIDERS if p["category"] == "sms" or p["provider_type"] == "sms"]
        if capability == "extract":
            return [p for p in PROVIDERS if p["category"] in ("extract", "extract_pay") or p["provider_type"] in ("extract", "extract_pay")]
        if capability == "pay":
            return [p for p in PROVIDERS if p["category"] in ("pay", "extract_pay") or p["provider_type"] in ("pay", "extract_pay")]
    return PROVIDERS


def get_provider(pid: str):
    if pid == "local_zero_link":
        pid = "local"
    return next((p for p in PROVIDERS if p["id"] == pid), None)


def supports_provider_proxy(pid: str) -> bool:
    # 只开放已有公开文档证据的内置代理，不推测占位供应商的能力。
    return pid in ("ytc", "team_free_extract")
