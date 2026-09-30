#!/usr/bin/env python3
"""Write the Sparkle appcast for one release: appcast.xml, one item.

    python3 packaging/appcast.py --version 0.6.0 --dmg dist/Syllabus-0.6.0.dmg \\
        --signature <sign_update -p output> --out dist/appcast.xml

The release workflow runs this after the disk image is signed, notarized, and
stapled (stapling changes the file, so the EdDSA signature has to come after),
and attaches appcast.xml to the release. The app's SUFeedURL points at
releases/latest/download/appcast.xml, so the newest release's feed is the one
every copy reads. One item is enough: Sparkle only needs to know the newest.

Also:
    python3 packaging/appcast.py --public-key-from-env SPARKLE_ED_PRIVATE_KEY
prints the base64 public key for the private key in that variable (the
32-byte seed `generate_keys -x` exports), which is what goes into Info.plist.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REPO = "SyllabusAI/LectureAI"
MIN_SYSTEM = "13.0"  # LSMinimumSystemVersion in syllabus.spec


def dmg_url(version: str, name: str, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}/{name}"


def build(version: str, url: str, length: int, signature: str,
          notes_url: str = "", pub_date: str | None = None,
          min_system: str = MIN_SYSTEM) -> bytes:
    if not signature.strip():
        raise ValueError("no EdDSA signature")
    ET.register_namespace("sparkle", SPARKLE_NS)
    sp = lambda tag: f"{{{SPARKLE_NS}}}{tag}"  # noqa: E731
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Syllabus"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Syllabus {version}"
    ET.SubElement(item, "pubDate").text = pub_date or formatdate(usegmt=True)
    ET.SubElement(item, sp("version")).text = version  # CFBundleVersion
    ET.SubElement(item, sp("shortVersionString")).text = version
    ET.SubElement(item, sp("minimumSystemVersion")).text = min_system
    if notes_url:
        ET.SubElement(item, sp("fullReleaseNotesLink")).text = notes_url
    ET.SubElement(item, "enclosure", {
        "url": url,
        "type": "application/octet-stream",
        sp("edSignature"): signature.strip(),
        "length": str(length),
    })
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def public_key(private_b64: str) -> str:
    """The Ed25519 public key, base64, for Sparkle's exported private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = base64.b64decode(private_b64.strip(), validate=True)
    if len(raw) != 32:
        raise ValueError(f"expected the 32-byte seed from `generate_keys -x`, got {len(raw)} bytes")
    key = Ed25519PrivateKey.from_private_bytes(raw).public_key()
    return base64.b64encode(key.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--public-key-from-env", metavar="VAR")
    parser.add_argument("--version")
    parser.add_argument("--dmg", type=Path)
    parser.add_argument("--signature", help="the output of `sign_update -p <dmg>`")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.public_key_from_env:
        print(public_key(os.environ.get(args.public_key_from_env, "")))
        return 0
    if not (args.version and args.dmg and args.signature and args.out):
        parser.error("--version, --dmg, --signature, and --out are required")
    xml = build(args.version, dmg_url(args.version, args.dmg.name, args.repo),
                args.dmg.stat().st_size, args.signature,
                notes_url=f"https://github.com/{args.repo}/releases/tag/v{args.version}")
    args.out.write_bytes(xml)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
