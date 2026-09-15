"""HTTPS 自签证书自动生成 — 纯 Python 实现，无需 openssl/bash，跨平台。"""
import os
import ssl
import socket
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("magic")

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


def _generate_with_cryptography(cert_path: str, key_path: str, cn: str) -> bool:
    """用 cryptography 库生成自签证书（10 年有效）。"""
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(cn), x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        os.chmod(key_path, 0o600)
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        log.info(f"[cert] 证书已生成: {cert_path} / {key_path} (CN={cn}, 10年有效)")
        return True
    except Exception as e:
        log.error(f"[cert] 生成失败: {e}")
        return False


def ensure_cert(cert_path: str, key_path: str, script_path: str | None = None) -> bool:
    """检测证书缺失则自动生成自签证书。

    优先用 cryptography 库（纯 Python）；
    若未安装则回退到 openssl 子进程（macOS/Linux）；
    Windows 无 openssl 时无法生成，返回 False。
    """
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return True

    cn = socket.gethostname()
    log.info(f"[cert] 证书缺失，自动生成 (CN={cn})...")

    # 优先：cryptography 库（跨平台，无外部依赖）
    if _HAS_CRYPTO:
        return _generate_with_cryptography(cert_path, key_path, cn)

    # 回退：openssl 子进程（macOS/Linux）
    if script_path is None:
        script_path = os.path.join(os.getcwd(), "scripts", "gen-cert.sh")
    if os.path.exists(script_path):
        try:
            import subprocess
            cert_dir = os.path.dirname(cert_path) or "."
            subprocess.run(
                ["bash", script_path, cert_dir, cn],
                check=True, capture_output=True, text=True,
            )
            if os.path.exists(cert_path) and os.path.exists(key_path):
                log.info(f"[cert] 证书已生成: {cert_path} / {key_path}")
                return True
            log.error("[cert] gen-cert.sh 执行完但证书文件未出现")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error(f"[cert] 生成失败: {e}")
    else:
        log.error(f"[cert] openssl 未安装且 {script_path} 不存在，请手动生成证书")
    return False
