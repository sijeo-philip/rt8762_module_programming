"""Golden Rig certificate tool (docs/operator_supervisor_guide.md, Part A).

Creates, for a Golden Rig with the ID and network name the technician
chooses:

* the line's CA (once) - or reuses an existing one,
* the rig's certificate and key, made for exactly that rig ID (CN) and
  name(s) (subjectAltName),
* the SHA-256 fingerprint, written exactly as the station shows it on the
  Golden Rig binding screen,

and proves the result with the same TLS checks the station makes
(CA trust, host name, TLS 1.2+), before anything is handed out.

    GoldenRigCertTool.exe                         (asks the questions)
    GoldenRigCertTool.exe --rig-id GOLDEN-RIG-01 --name golden-rig-01 [--ip 192.168.10.50]
    python -m stationapp.tools.golden_rig_certs --help      (development)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import re
import shutil
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_CERT = "golden_rig_ca.pem"
CA_KEY = "golden_rig_ca.key"
RIG_CERT = "rig.pem"
RIG_KEY = "rig.key"
INFO_FILE = "RIG-INFO.txt"
CA_COMMON_NAME = "Golden Rig CA"
KEY_BITS = 3072
CA_DAYS = 3650
RIG_DAYS = 1095
DEFAULT_PORT = 8762

_RIG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


class CertToolError(Exception):
    """A problem the technician must fix (shown without a traceback)."""


# --------------------------------------------------------------------------
# Input checks
# --------------------------------------------------------------------------

def check_rig_id(value: str) -> str:
    value = value.strip()
    if not _RIG_ID.match(value):
        raise CertToolError(
            f"Rig ID {value!r}: use 1-64 letters, digits, '-', '_' or '.', e.g. GOLDEN-RIG-01"
        )
    return value


def default_host_name(rig_id: str) -> str:
    """GOLDEN-RIG-01 -> golden-rig-01 (a valid Windows computer name)."""
    return re.sub(r"[^a-z0-9-]+", "-", rig_id.lower()).strip("-")[:63] or "golden-rig"


def dns_names_for(name: str) -> list[str]:
    """Names to put in the certificate for the rig's network name.

    A bare computer name gets its mDNS form too: "golden-rig-01" ->
    ["golden-rig-01.local", "golden-rig-01"]. A full name is kept as
    given; "x.local" also gets the bare "x".
    """
    name = name.strip().lower().rstrip(".")
    if not name:
        raise CertToolError("The rig's network name is required, e.g. golden-rig-01")
    if name.startswith(("http://", "https://")) or ":" in name or "/" in name:
        raise CertToolError(f"Name {name!r}: type only the name, without http:// or :port")
    labels = name.split(".")
    if not all(_LABEL.match(label) for label in labels):
        raise CertToolError(
            f"Name {name!r}: use letters, digits and '-' only (dots between parts), e.g. golden-rig-01"
        )
    if len(labels) == 1:
        return [f"{name}.local", name]
    if labels[-1] == "local" and len(labels) == 2:
        return [name, labels[0]]
    return [name]


def check_ips(values: list[str]) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    result = []
    for value in values:
        for item in value.replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                result.append(ipaddress.ip_address(item))
            except ValueError as exc:
                raise CertToolError(f"{item!r} is not an IP address") from exc
    return result


# --------------------------------------------------------------------------
# Certificates
# --------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def create_ca(ca_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """A new line CA, same profile as the OpenSSL commands in the guide."""
    ca_dir.mkdir(parents=True, exist_ok=True)
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    start = _now() - dt.timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(ca_dir / CA_KEY, key)
    _write_cert(ca_dir / CA_CERT, cert)
    return cert, key


def load_ca(ca_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    cert_path, key_path = ca_dir / CA_CERT, ca_dir / CA_KEY
    if not key_path.is_file():
        raise CertToolError(
            f"{cert_path} exists but its key {key_path} is missing. A rig certificate can only "
            f"be signed with the CA key: put {CA_KEY} next to it, or choose another CA folder."
        )
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise CertToolError(f"Cannot read the CA in {ca_dir}: {exc}") from exc
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise CertToolError(f"{CA_KEY} does not belong to {CA_CERT} in {ca_dir}")
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    if not constraints.ca:
        raise CertToolError(f"{cert_path} is not a CA certificate")
    if cert.not_valid_after_utc <= _now():
        raise CertToolError(f"The CA {cert_path} expired on {cert.not_valid_after_utc:%Y-%m-%d}")
    return cert, key


def create_rig_certificate(
    rig_dir: Path,
    *,
    rig_id: str,
    dns_names: list[str],
    ips: list,
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    days: int = RIG_DAYS,
) -> x509.Certificate:
    rig_dir.mkdir(parents=True, exist_ok=True)
    key = _new_key()
    start = _now() - dt.timedelta(minutes=5)
    end = min(start + dt.timedelta(days=days), ca_cert.not_valid_after_utc)
    san = [x509.DNSName(name) for name in dns_names] + [x509.IPAddress(ip) for ip in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, rig_id)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(rig_dir / RIG_KEY, key)
    _write_cert(rig_dir / RIG_CERT, cert)
    return cert


def station_fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 over the DER certificate, as the station displays it."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def openssl_fingerprint(cert: x509.Certificate) -> str:
    """The same value as `openssl x509 -fingerprint -sha256` prints it."""
    digest = station_fingerprint(cert).upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# --------------------------------------------------------------------------
# Station-equivalent check
# --------------------------------------------------------------------------

def verify_like_station(ca_file: Path, cert_file: Path, key_file: Path, server_name: str) -> str:
    """TLS handshake in memory with the station's client settings.

    Returns the CN the station would read as the rig ID; raises
    CertToolError with the station's reason when it would refuse.
    """
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.load_cert_chain(str(cert_file), str(key_file))

    client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.check_hostname = True
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=server_name)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)

    done = {"client": False, "server": False}
    try:
        for _ in range(50):
            for label, side in (("client", client), ("server", server)):
                if done[label]:
                    continue
                try:
                    side.do_handshake()
                    done[label] = True
                except ssl.SSLWantReadError:
                    pass
            s_in.write(c_out.read())
            c_in.write(s_out.read())
            if all(done.values()):
                break
        else:
            raise CertToolError(f"TLS handshake for {server_name} did not complete")
    except ssl.SSLCertVerificationError as exc:
        raise CertToolError(
            f"The station would refuse this rig at {server_name!r}: {exc.verify_message}"
        ) from exc
    except ssl.SSLError as exc:
        raise CertToolError(f"TLS check for {server_name!r} failed: {exc}") from exc

    subject = dict(item[0] for item in client.getpeercert()["subject"])
    return subject.get("commonName", "")


# --------------------------------------------------------------------------
# Hand-out notes
# --------------------------------------------------------------------------

def write_info(
    path: Path,
    *,
    rig_id: str,
    dns_names: list[str],
    ips: list,
    cert: x509.Certificate,
    ca_dir: Path,
    port: int,
) -> None:
    name = dns_names[0]
    addresses = ", ".join([*dns_names, *(str(ip) for ip in ips)])
    text = f"""GOLDEN RIG CERTIFICATE - {rig_id}
Created {_now():%Y-%m-%d %H:%M} UTC with the Station App Golden Rig certificate tool.

Rig ID (certificate CN) : {rig_id}
Names / IPs in the cert : {addresses}
Valid until             : {cert.not_valid_after_utc:%Y-%m-%d} (put this in the maintenance calendar)
Port                    : {port}

SHA-256 FINGERPRINT (as the station shows it - give this to the Supervisors):
  {station_fingerprint(cert)}
Same value as OpenSSL prints it:
  {openssl_fingerprint(cert)}

FILES
  {RIG_CERT}   -> this Golden Rig only (not secret)
  {RIG_KEY}    -> this Golden Rig only. SECRET: readable only by the rig service account.
  {CA_CERT}  -> every station: <StationApp folder>\\config\\certificates\\{CA_CERT}
               (CA folder: {ca_dir})
  {CA_KEY}  -> NOWHERE. Keep it offline (encrypted USB stick, locked cabinet).

ON THE RIG PC
  1. Computer name: {name.split('.')[0]}   (Settings -> System -> About -> Rename this PC)
  2. Copy {RIG_CERT} and {RIG_KEY} to C:\\GoldenRig\\certs\\
  3. Start the rig service with exactly this rig ID:
       python golden_rig_server.py --rig-id {rig_id} --cert C:\\GoldenRig\\certs\\{RIG_CERT} --key C:\\GoldenRig\\certs\\{RIG_KEY} --port {port}
  4. Firewall: TCP {port} inbound, UDP 5353 (mDNS); network profile Private.

ON EVERY STATION (Supervisor, guide step C4)
  Station configuration -> Golden Rig binding
    mDNS name or IP : {name}
    Port            : {port}
  Request identity -> the rig ID must be {rig_id} and the SHA-256 must match the
  fingerprint above (at least the first and last 8 characters). Then approve.
"""
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")


# --------------------------------------------------------------------------
# Command line / interactive
# --------------------------------------------------------------------------

@dataclass
class Request:
    rig_id: str
    dns_names: list[str]
    ips: list
    out_dir: Path
    ca_dir: Path
    port: int = DEFAULT_PORT
    days: int = RIG_DAYS
    force: bool = False
    install_ca: Path | None = None


def run(request: Request, *, confirm=lambda question: False) -> Path:
    """Create (or reuse) the CA, issue the rig certificate, verify it.

    Returns the rig's folder.
    """
    rig_dir = request.out_dir / request.rig_id
    if (rig_dir / RIG_CERT).exists() and not request.force:
        if not confirm(f"{rig_dir / RIG_CERT} already exists. Replace it (the rig must then be re-approved on every station)?"):
            raise CertToolError(f"Not replaced: {rig_dir / RIG_CERT}")

    if (request.ca_dir / CA_CERT).exists():
        ca_cert, ca_key = load_ca(request.ca_dir)
        print(f"Using the existing CA:   {request.ca_dir / CA_CERT}")
    else:
        ca_cert, ca_key = create_ca(request.ca_dir)
        print(f"Created a new CA:        {request.ca_dir / CA_CERT}  (valid {CA_DAYS // 365} years)")

    cert = create_rig_certificate(
        rig_dir, rig_id=request.rig_id, dns_names=request.dns_names, ips=request.ips,
        ca_cert=ca_cert, ca_key=ca_key, days=request.days,
    )
    if cert.not_valid_after_utc < _now() + dt.timedelta(days=request.days - 1):
        print(f"NOTE: limited to the CA's expiry, {cert.not_valid_after_utc:%Y-%m-%d}.")

    # Prove it exactly as a station will see it, for every name and IP.
    for target in [*request.dns_names, *(str(ip) for ip in request.ips)]:
        cn = verify_like_station(request.ca_dir / CA_CERT, rig_dir / RIG_CERT, rig_dir / RIG_KEY, target)
        if cn != request.rig_id:
            raise CertToolError(f"Check failed: certificate CN is {cn!r}, expected {request.rig_id!r}")
        print(f"Station check OK:        {target} -> rig ID {cn}")

    shutil.copy2(request.ca_dir / CA_CERT, rig_dir / CA_CERT)
    write_info(
        rig_dir / INFO_FILE, rig_id=request.rig_id, dns_names=request.dns_names, ips=request.ips,
        cert=cert, ca_dir=request.ca_dir, port=request.port,
    )

    if request.install_ca is not None:
        install_station_ca(request.ca_dir / CA_CERT, request.install_ca)
    return rig_dir


def install_station_ca(ca_file: Path, station_dir: Path) -> None:
    target = station_dir / "config" / "certificates" / CA_CERT
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() == ca_file.read_bytes():
            print(f"This station already trusts this CA: {target}")
            return
        backup = target.with_name(f"{CA_CERT}.{_now():%Y%m%d-%H%M%S}.bak")
        target.replace(backup)
        print(f"Old station CA kept as:  {backup}")
    shutil.copy2(ca_file, target)
    print(f"Station CA installed:    {target}  (restart Station App)")


def _station_dir() -> Path | None:
    """The StationApp folder, when the tool runs from an installed station."""
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
        if (folder / "StationApp.exe").exists():
            return folder
    return None


def _default_out_dir() -> Path:
    return Path.home() / "Documents" / "GoldenRigCerts"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    # Strip a byte-order mark: answers piped from PowerShell start with one.
    answer = input(f"{prompt}{suffix}: ")
    for mark in ("\ufeff", "\u00ef\u00bb\u00bf"):  # UTF-8 BOM, raw or as cp1252
        answer = answer.replace(mark, "")
    answer = answer.strip()
    return answer or default


def _yes(prompt: str, default: bool = False) -> bool:
    answer = _ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return answer.startswith("y")


def interactive() -> Request:
    print(__doc__.split("\n\n")[0])
    print()
    print("Answer the questions; press Enter to accept the value in [brackets].\n")
    while True:
        try:
            rig_id = check_rig_id(_ask("Rig ID (certificate CN), e.g. GOLDEN-RIG-01"))
            break
        except CertToolError as exc:
            print("  ", exc)
    while True:
        try:
            name = _ask("Rig PC name (stations connect to <name>.local)", default_host_name(rig_id))
            dns_names = dns_names_for(name)
            break
        except CertToolError as exc:
            print("  ", exc)
    while True:
        try:
            ips = check_ips([_ask("Fixed IP address(es) of the rig, comma separated (blank = none)")])
            break
        except CertToolError as exc:
            print("  ", exc)
    out_dir = Path(_ask("Output folder", str(_default_out_dir())))
    print(f"\nThe CA (shared by every rig of the line) is kept in a folder with {CA_CERT} + {CA_KEY}.")
    print("If your line already has a CA, give its folder; otherwise a new CA is created.")
    ca_dir = Path(_ask("CA folder", str(out_dir / "CA")))

    station = _station_dir()
    install_ca = None
    if station is not None and _yes(f"Also install the CA on this station ({station})?", True):
        install_ca = station

    print("\nSummary")
    print(f"  Rig ID      : {rig_id}")
    print(f"  Names       : {', '.join(dns_names)}")
    print(f"  IPs         : {', '.join(map(str, ips)) or '(none)'}")
    print(f"  Rig files   : {out_dir / rig_id}")
    print(f"  CA          : {ca_dir}")
    if not _yes("Create the certificate now?", True):
        raise CertToolError("Cancelled.")
    return Request(rig_id=rig_id, dns_names=dns_names, ips=ips, out_dir=out_dir, ca_dir=ca_dir, install_ca=install_ca)


def parse_args(argv: list[str]) -> Request:
    parser = argparse.ArgumentParser(
        prog="GoldenRigCertTool",
        description="Create the CA (once) and a Golden Rig certificate, key and fingerprint "
                    "for the rig ID and name you choose. Without options it asks the questions.",
    )
    parser.add_argument("--rig-id", required=True, help="rig ID = certificate CN, e.g. GOLDEN-RIG-01")
    parser.add_argument("--name", help="rig PC name, e.g. golden-rig-01 (default: from the rig ID); "
                                       "golden-rig-01 also covers golden-rig-01.local")
    parser.add_argument("--ip", action="append", default=[], help="fixed IP of the rig (repeat for more)")
    parser.add_argument("--out", type=Path, default=_default_out_dir(), help="output folder")
    parser.add_argument("--ca-dir", type=Path, help=f"folder with {CA_CERT} + {CA_KEY} "
                                                    "(default: <out>\\CA; created if empty)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="rig port shown in the notes")
    parser.add_argument("--days", type=int, default=RIG_DAYS, help="rig certificate validity in days")
    parser.add_argument("--force", action="store_true", help="replace an existing rig certificate")
    parser.add_argument("--install-ca", type=Path, metavar="STATION_DIR",
                        help="also copy the CA to STATION_DIR\\config\\certificates")
    args = parser.parse_args(argv)
    rig_id = check_rig_id(args.rig_id)
    return Request(
        rig_id=rig_id,
        dns_names=dns_names_for(args.name or default_host_name(rig_id)),
        ips=check_ips(args.ip),
        out_dir=args.out,
        ca_dir=args.ca_dir or args.out / "CA",
        port=args.port,
        days=args.days,
        force=args.force,
        install_ca=args.install_ca,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    asked = not argv
    try:
        request = interactive() if asked else parse_args(argv)
        rig_dir = run(request, confirm=_yes if asked else (lambda question: False))
        cert = x509.load_pem_x509_certificate((rig_dir / RIG_CERT).read_bytes())
        print()
        print(f"Done. Files for {request.rig_id}: {rig_dir}")
        print(f"  SHA-256 fingerprint (as the station shows it): {station_fingerprint(cert)}")
        print(f"  Valid until {cert.not_valid_after_utc:%Y-%m-%d}.  Hand-out notes: {rig_dir / INFO_FILE}")
        print(f"  Keep {request.ca_dir / CA_KEY} OFFLINE; it never goes on a station or a rig.")
        code = 0
    except CertToolError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        code = 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        code = 1
    if asked:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
