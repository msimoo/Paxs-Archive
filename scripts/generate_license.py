#!/usr/bin/env python3
"""PAXS Vendor License Generation & Key Management CLI.

Usage:
  # 1. Generate or re-generate vendor RSA key pair
  python scripts/generate_license.py init-keys

  # 2. Issue a Hospital license bound to a machine fingerprint
  python scripts/generate_license.py create \
    --customer "Metro General Hospital" \
    --edition hospital \
    --fingerprint "F55DF4029D231578E6E0CD5A1E07404E" \
    --days 365 \
    --max-users 100 \
    --max-studies 250000 \
    --output paxs.lic

  # 3. Issue a Developer / Cloud Wildcard Perpetual License
  python scripts/generate_license.py create \
    --customer "Internal Dev" \
    --edition enterprise \
    --fingerprint "*" \
    --perpetual \
    --output paxs.lic

  # 4. Verify / inspect a license file
  python scripts/generate_license.py verify paxs.lic

  # 5. Show this machine's hardware fingerprint
  python scripts/generate_license.py fingerprint
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# These imports must follow the sys.path bootstrap above, so the script can be
# run straight from a checkout without installing the package (hence E402).
from app.core.licensing.crypto import (  # noqa: E402
    LicenseData,
    LicenseError,
    RevokedSigningKey,
    assert_license_signature,
    decrypt_license_container,
    encrypt_license_container,
    generate_rsa_key_pair,
    sign_license,
)
from app.core.licensing.fingerprint import (  # noqa: E402
    get_hardware_info,
    get_machine_fingerprint,
)

KEYS_DIR = ROOT_DIR / "scripts" / "vendor_keys"
PRIVATE_KEY_PATH = KEYS_DIR / "paxs_vendor_private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "paxs_vendor_public.pem"


def load_signing_key() -> bytes:
    """The vendor signing key: secrets manager first, then the git-ignored file.

    B-2/FS-7: prefer ``PAXS_LICENSE_SIGNING_KEY`` (PEM in an env var injected by
    Vault/KMS) or ``PAXS_LICENSE_SIGNING_KEY_PATH`` (a mounted secret). The
    on-disk fallback is the git-ignored ``scripts/vendor_keys/`` directory, which
    exists only on the vendor's own machine. Nothing is ever bundled in source.
    """
    env_pem = os.environ.get("PAXS_LICENSE_SIGNING_KEY")
    if env_pem:
        return env_pem.encode("utf-8")
    env_path = os.environ.get("PAXS_LICENSE_SIGNING_KEY_PATH")
    if env_path and Path(env_path).is_file():
        return Path(env_path).read_bytes()
    if PRIVATE_KEY_PATH.exists():
        return PRIVATE_KEY_PATH.read_bytes()
    print("[!] No vendor signing key configured.")
    print("    Set PAXS_LICENSE_SIGNING_KEY (PEM) or PAXS_LICENSE_SIGNING_KEY_PATH,")
    print("    or run `init-keys` to create one in the git-ignored keys directory.")
    sys.exit(1)


def cmd_init_keys(args):
    """Generate vendor RSA-4096 key pair."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_KEY_PATH.exists() and not args.force:
        print(f"[!] Private key already exists at {PRIVATE_KEY_PATH}. Use --force to overwrite.")
        return

    priv_pem, pub_pem = generate_rsa_key_pair()
    PRIVATE_KEY_PATH.write_bytes(priv_pem)
    PUBLIC_KEY_PATH.write_bytes(pub_pem)
    print("[OK] Generated vendor RSA-4096 keys:")
    print(f"    Private: {PRIVATE_KEY_PATH}   <-- NEVER commit this")
    print(f"    Public:  {PUBLIC_KEY_PATH}")
    print("\n[!] Move the private key into your secrets manager and delete it from disk.")
    print("    The directory is git-ignored; confirm with:")
    print("      git check-ignore -v scripts/vendor_keys/paxs_vendor_private.pem")
    print("\n[!] Then paste the public key into TRUSTED_PUBLIC_KEY_PEM in")
    print("    app/core/licensing/crypto.py and PAXS_VENDOR_PUBLIC_KEY in the portal")
    print("    config. See docs/SECURITY-KEY-ROTATION.md.")
    print("\nPublic Key PEM:\n" + pub_pem.decode("ascii"))


def cmd_fingerprint(args):
    """Show machine fingerprint."""
    fp = get_machine_fingerprint()
    info = get_hardware_info()
    print("=" * 60)
    print("  PAXS Server Machine Hardware Fingerprint")
    print("=" * 60)
    print(f"  Fingerprint:  {fp}")
    print(f"  OS:           {info.get('os')}")
    print(f"  Hostname:     {info.get('hostname')}")
    print(f"  MAC Addr:     {info.get('mac_addresses')}")
    if info.get("cpu_id"):
        print(f"  CPU ID:       {info.get('cpu_id')}")
    if info.get("motherboard_serial"):
        print(f"  Motherboard:  {info.get('motherboard_serial')}")
    if info.get("os_serial"):
        print(f"  OS Serial:    {info.get('os_serial')}")
    print("=" * 60)


def cmd_create(args):
    """Create and sign a new license file."""
    priv_pem = load_signing_key()

    now = datetime.now(timezone.utc)
    expires_at = None if args.perpetual else (now + timedelta(days=args.days))

    license_id = args.license_id or f"PAXS-{args.edition.upper()}-{now.year}-{os.urandom(2).hex().upper()}-{os.urandom(2).hex().upper()}"

    edition_features = {
        "trial": ["recist", "lung_rads"],
        "clinic": ["recist", "lung_rads", "agatston"],
        "hospital": ["recist", "lung_rads", "agatston", "pe_clot", "breast_density", "hl7_router"],
        "enterprise": ["recist", "lung_rads", "agatston", "pe_clot", "breast_density", "hl7_router", "fhir_gateway", "storage_tiering", "ai_pipeline"],
    }
    features = list(set(edition_features.get(args.edition.lower(), []) + (args.features.split(",") if args.features else [])))

    lic_data = LicenseData(
        license_id=license_id,
        version="1.0",
        customer_name=args.customer,
        customer_email=args.email,
        customer_organization=args.org or args.customer,
        customer_country=args.country,
        product="PAXS Archive & Viewer",
        edition=args.edition.lower(),
        machine_fingerprint=args.fingerprint,
        issued_at=now,
        expires_at=expires_at,
        max_users=args.max_users,
        max_studies_per_year=args.max_studies,
        features=features,
    )

    # Sign with RSA private key
    signed_data = sign_license(lic_data, priv_pem)

    # Encrypt into AES-GCM binary container
    container_bytes = encrypt_license_container(signed_data)

    out_path = Path(args.output)
    out_path.write_bytes(container_bytes)

    print("=" * 60)
    print(f" [OK] License successfully created and saved to: {out_path}")
    print("=" * 60)
    print(f"  License ID:    {signed_data.license_id}")
    print(f"  Customer:      {signed_data.customer_name}")
    print(f"  Edition:       {signed_data.edition.upper()}")
    print(f"  Fingerprint:   {signed_data.machine_fingerprint}")
    print(f"  Expires:       {signed_data.expires_at.isoformat() if signed_data.expires_at else 'PERPETUAL'}")
    print(f"  Max Users:     {signed_data.max_users}")
    print(f"  Max Studies:   {signed_data.max_studies_per_year:,}/yr")
    print(f"  Features:      {', '.join(signed_data.features)}")
    print("=" * 60)


def cmd_verify(args):
    """Verify and inspect a .lic file."""
    path = Path(args.license_file)
    if not path.exists():
        print(f"[!] File not found: {path}")
        sys.exit(1)

    raw_bytes = path.read_bytes()
    try:
        data = decrypt_license_container(raw_bytes)
    except Exception as exc:
        print(f"[ERR] Decryption failed: {exc}")
        sys.exit(1)

    # Verify signature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    pub_key = None
    if PUBLIC_KEY_PATH.exists():
        pub_key = serialization.load_pem_public_key(PUBLIC_KEY_PATH.read_bytes(), backend=default_backend())

    # Report *why* a signature failed — a revoked signing key needs a different
    # remedy (ask the vendor for a re-issued licence) from a corrupt file.
    sig_ok, sig_note = True, ""
    try:
        assert_license_signature(data, pub_key)
    except RevokedSigningKey as exc:
        sig_ok, sig_note = False, f"\n                       {exc}"
    except LicenseError as exc:
        sig_ok, sig_note = False, f"\n                       {exc}"

    current_fp = get_machine_fingerprint()
    fp_match = (data.machine_fingerprint == "*" or data.machine_fingerprint == current_fp)

    is_expired = False
    if data.expires_at:
        is_expired = datetime.now(timezone.utc) > data.expires_at

    print("=" * 60)
    print(f"  PAXS License Inspection: {path.name}")
    print("=" * 60)
    print(f"  Signature Valid:     {'[OK] YES' if sig_ok else '[ERR] INVALID / TAMPERED'}{sig_note}")
    print(f"  Hardware Match:      {'[OK] YES' if fp_match else '[MISMATCH] (Target: ' + data.machine_fingerprint + ', Host: ' + current_fp + ')'}")
    print(f"  Expiration Status:   {'[EXPIRED]' if is_expired else '[ACTIVE]'}")
    print("-" * 60)
    print(f"  License ID:          {data.license_id}")
    print(f"  Customer Name:       {data.customer_name}")
    print(f"  Organization:        {data.customer_organization or '-'}")
    print(f"  Edition:             {data.edition.upper()}")
    print(f"  Issued:              {data.issued_at.isoformat()}")
    print(f"  Expires:             {data.expires_at.isoformat() if data.expires_at else 'PERPETUAL'}")
    print(f"  Max Users:           {data.max_users}")
    print(f"  Max Studies / Year:  {data.max_studies_per_year:,}")
    print(f"  Features:            {', '.join(data.features)}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="PAXS Vendor License Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-keys
    p_init = subparsers.add_parser("init-keys", help="Generate vendor RSA key pair")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing keys")

    # fingerprint
    subparsers.add_parser("fingerprint", help="Show machine hardware fingerprint")

    # create
    p_create = subparsers.add_parser("create", help="Create a signed license file")
    p_create.add_argument("--customer", required=True, help="Customer name")
    p_create.add_argument("--email", help="Customer email")
    p_create.add_argument("--org", help="Customer organization")
    p_create.add_argument("--country", help="Country")
    p_create.add_argument("--edition", default="hospital", choices=["trial", "clinic", "hospital", "enterprise"], help="License edition")
    p_create.add_argument("--fingerprint", default="*", help="Hardware fingerprint (or '*' for cloud/wildcard)")
    p_create.add_argument("--days", type=int, default=365, help="Validity period in days")
    p_create.add_argument("--perpetual", action="store_true", help="Make license perpetual (no expiry)")
    p_create.add_argument("--max-users", type=int, default=100, help="Max user seats")
    p_create.add_argument("--max-studies", type=int, default=100000, help="Max studies per year")
    p_create.add_argument("--features", help="Comma-separated feature list override")
    p_create.add_argument("--license-id", help="Custom license ID")
    p_create.add_argument("--output", default="paxs.lic", help="Output .lic filepath")

    # verify
    p_verify = subparsers.add_parser("verify", help="Inspect and verify a .lic file")
    p_verify.add_argument("license_file", help="Path to .lic file")

    args = parser.parse_args()
    if args.command == "init-keys":
        cmd_init_keys(args)
    elif args.command == "fingerprint":
        cmd_fingerprint(args)
    elif args.command == "create":
        cmd_create(args)
    elif args.command == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
