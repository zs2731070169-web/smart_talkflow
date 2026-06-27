"""api-key 与 api-key-hash 生成工具。

供运维/部署生成 Agent 服务账号凭证:

- :func:`generate_api_key`:生成明文 api-key(CSPRNG,32 字节 base64url)。填 talkflow
  ``.env`` 的 ``OA_API_KEY``。
- :func:`api_key_hash`:算 SHA-256 hex(对应 yudao ``yudao.agent.api-key-hash``)。

哈希算法与 yudao ``AgentDelegationFilter`` 完全一致 —— Java 侧
``MessageDigest.getInstance("SHA-256").digest(apiKey.getBytes(UTF_8))`` + hex 编码,
等价于本模块的 ``hashlib.sha256(api_key.encode("utf-8")).hexdigest()``。

命令行用法(项目根)::

    PYTHONPATH=src python -m utils.api_key_util                 # 生成一对 api-key + hash
    PYTHONPATH=src python -m utils.api_key_util --api-key <明文> # 给定明文算 hash
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys


def generate_api_key(num_bytes: int = 32) -> str:
    """生成明文 api-key(密码学安全随机,base64url,默认 32 字节 ≈ 43 字符)。

    :param num_bytes: 随机字节数(建议 ≥32,即 ≥256 bit)。
    """
    return secrets.token_urlsafe(num_bytes)


def api_key_hash(api_key: str) -> str:
    """计算 api-key 的 SHA-256 hex(对应 yudao ``yudao.agent.api-key-hash``)。

    与 yudao ``AgentDelegationFilter`` 的 SHA-256 + UTF-8 + hex 编码一致,
    保证「Python 算的 hash」与「yudao 存的 hash」可恒定时间比对。
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _main() -> int:
    parser = argparse.ArgumentParser(description="生成 Agent 服务账号 api-key + SHA-256 hash(yudao api-key-hash)")
    parser.add_argument(
        "--api-key",
        help="给定明文 api-key 算其 hash;不填则新生成一对(api-key + hash)",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=32,
        help="生成 api-key 的随机字节数(默认 32)",
    )
    args = parser.parse_args()

    api_key = args.api_key if args.api_key else generate_api_key(args.bytes)
    digest = api_key_hash(api_key)

    print("明文 api-key  → 填 talkflow .env 的 OA_API_KEY:")
    print(f"  {api_key}")
    print()
    print("api-key-hash  → 填 yudao application.yaml 的 yudao.agent.api-key-hash:")
    print(f"  {digest}")
    print()
    print("校验(应与上面 hash 一致):")
    print(f"  echo -n '{api_key}' | openssl dgst -sha256")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
